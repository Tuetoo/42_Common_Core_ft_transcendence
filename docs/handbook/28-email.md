# 28. Email

> AlphaMind sends a small number of emails: an email-address verification
> link, a password reset link, an organization invitation link, optional
> copies of in-app notifications, and (from a separate program) monitoring
> alerts. All application emails go through one module,
> `shared/email.py`, which speaks plain SMTP with the `aiosmtplib`
> library. In development every email lands in MailHog, a fake mail
> server with a web page and a JSON API on `http://127.0.0.1:8025`. In
> production you point the `SMTP_*` settings at a real provider such as
> Brevo; no code changes. Emails are always sent after the database
> commit, in a background task, and a failed send is logged but never
> breaks the request that caused it.

---

## 1. The big picture

```
  Browser / API client
        |
        v  https://localhost:8443  (gateway)
  +----------------------+          +------------------------------+
  | auth-service         |          | core-service                 |
  |  register            |          |  notify_user / notify_users  |
  |  verify-email/resend |          |  (only if the user turned    |
  |  password-reset      |          |   on the "email" channel)    |
  |  admin email change  |          +------------------------------+
  |  org invitation      |                       |
  +----------------------+                       |
        |  BackgroundTasks, after the commit     |
        v                                        v
     shared/email.py  (send_email -> aiosmtplib.send)
        |
        |  SMTP to SMTP_HOST:SMTP_PORT
        v
  development: mailhog:1025  ----> web UI + API on 127.0.0.1:8025
  production:  a real SMTP relay (Brevo, SES, SendGrid, Mailgun, ...)

  Separate path (not shared/email.py):
  prometheus --alerts--> alertmanager --SMTP (infra/alertmanager/alertmanager.yml)--> mailhog:1025
```

The important ideas:

- There is **one sender** for application email (`shared/email.py`). It
  knows nothing about any provider. It only reads the `SMTP_*` settings.
- **Who sends what is fixed**: auth-service sends the account emails
  (verification, reset, invitation); core-service sends notification
  copies. ai-service never sends email.
- Emails are **side effects that happen after the work is saved**. The
  request first commits its database transaction, then the response goes
  to the client, then a FastAPI background task sends the email.
- Email is **best effort**. If the SMTP server is down, the user still
  gets a normal response; the failure is logged and the user can ask
  again (for example "resend verification").

---

## 2. Concepts you need

| Term | Meaning, and why it matters here |
|---|---|
| SMTP | Simple Mail Transfer Protocol: the standard protocol a program uses to hand an email to a mail server. AlphaMind only speaks SMTP, so any provider that offers an "SMTP relay" works. |
| SMTP relay | A mail server that accepts your message (usually after a login) and delivers it to the recipient's mailbox for you. Brevo, AWS SES, SendGrid and Mailgun all offer one. |
| `aiosmtplib` | An asyncio SMTP client library for Python. "Async" means sending does not block the event loop of the FastAPI service while it waits for the server. Pinned as `aiosmtplib>=3.0` in `pyproject.toml`. |
| MailHog | A fake SMTP server for development. It accepts every email on port 1025, stores it in memory, and shows it on a web page (port 8025). Nothing ever leaves your machine. |
| STARTTLS | A way to switch an SMTP connection that started as plain text to an encrypted one. `SMTP_USE_TLS=true` turns it on (it is passed as `start_tls` to `aiosmtplib.send`). |
| MIME multipart/alternative | An email format that carries the same message twice, as plain text and as HTML; the mail program shows the best one it can display. `shared/email.py` builds this format with Python's `email.message.EmailMessage`. |
| Transactional email | An email sent because of one user action (sign-up, reset), as opposed to newsletters. All AlphaMind application emails are transactional. |
| Background task | FastAPI's `BackgroundTasks`: a function the route registers; FastAPI runs it after the response was sent. Used here so the email never delays or breaks the response. |
| Single-purpose token | A short-lived JWT (a signed JSON token, see chapter 29) placed in the email link. Each kind has its own `type` claim, so a reset token cannot be used as a verification token. |
| Account enumeration | Finding out whether an email address has an account by watching how the server answers. The reset and resend routes give the same answer and the same timing either way. |

---

## 3. How it works in AlphaMind

### 3.1 The sender: `shared/email.py`

One function, `send_email(settings, *, to, subject, html_body,
text_body=None)` (the `*` means the other arguments must be passed by
name). It is a plain function that receives the settings on every call;
there is no sender object and no process-wide instance that could keep
old settings. `send_email`:
   - builds an `EmailMessage` with `From = SMTP_FROM_EMAIL`, `To`, and a
     cleaned `Subject` (`_clean_header` replaces any CR/LF with a space,
     so a subject can never inject extra mail headers);
   - sets the plain-text part to `text_body`, or, when none is given, to
     text made from the HTML (`_strip_html`). No template helper passes a
     `text_body`. `_strip_html` turns each `<a href="URL">TEXT</a>` into
     `TEXT: URL` (for example `Verify my email: https://...`), ends each
     paragraph (`</p>`, `<br>`) with a newline, removes the other tags,
     turns HTML entities back into characters (`html.unescape`), squeezes
     white space and drops empty lines. So a mail program that shows only
     plain text still shows a usable link;
   - adds the HTML part (`add_alternative(..., subtype="html")`);
   - calls `aiosmtplib.send(message, hostname=SMTP_HOST, port=SMTP_PORT,
     username=SMTP_USER or None, password=SMTP_PASSWORD or None,
     start_tls=SMTP_USE_TLS)`. An empty user or password means "no login"
     (MailHog needs none). No `timeout` is passed, so aiosmtplib's own
     default applies (60 seconds in the installed aiosmtplib 5.x);
   - on any exception logs `email_send_failed` (with `to`, `subject`,
     `error`) and raises `EmailSendError`; on success logs `email_sent`.
   - holds no connection: every call opens a fresh SMTP connection.

`send_quietly(failure_event, send, *args, **kwargs)` calls a template
helper and logs a failure as a warning named `failure_event` (with
`error`) instead of raising (section 3.5). It is meant for emails sent
after the response, where nobody could handle the error; the user
recovers by asking again (resend, new invitation).

On top of that there are four **template helpers**, one per kind of
email. All their arguments, `settings` included, are keyword-only:

| Helper | Subject | Body (HTML) |
|---|---|---|
| `send_verification_email(settings, to, verification_link, expire_hours)` | `Verify your AlphaMind account` | Welcome text, a "Verify my email" link, "This link expires in N hour(s)". |
| `send_password_reset_email(settings, to, reset_link, expire_hours)` | `Reset your AlphaMind password` | A "Reset my password" link, expiry, "your password will not be changed" if ignored. |
| `send_organization_invitation_email(settings, to, organization_name, invitation_link)` | `You're invited to join <organization> on AlphaMind` | The organization name in bold and an "Accept invitation" link. |
| `send_notification_email(settings, to, title, content)` | `AlphaMind: <title>` | The title and, if present, the content, each in a paragraph. |

Every value placed into the HTML goes through `_esc()`
(`html.escape(..., quote=True)`), quotes included, because links are
placed inside `href="..."` attributes. Subjects that contain a value (the
invitation's organization name, the notification title) also go through
`_clean_header`. `send_notification_email` has no fixed link or subject
shape, because notification types are open-ended
(`shared.enums.NotificationType`). The helpers take a finished link,
not a token: the caller builds the link from its own
`FRONTEND_BASE_URL`. That keeps `shared/email.py` free of any one
service's settings.

### 3.2 Every send site

There are exactly six places that cause an application email. You can
find them all by searching the code for the four helpers and for
`EmailDispatch` / `add_task`.

| # | Email | Route that triggers it | Code that builds it | Link | Link lifetime |
|---|---|---|---|---|---|
| 1 | Verification | `POST /api/v1/auth/register` | `AuthService.register` (`services/auth-service/domains/auth/service.py`) returns an `EmailDispatch(to, link)`; the route adds `send_verification_email_safely` | `{FRONTEND_BASE_URL}/verify-email?token=...` | `EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS` = 24 h |
| 2 | Verification (again) | `POST /api/v1/auth/verify-email/resend` | `AuthService.resend_verification` (returns `None` for an unknown, verified or inactive account) | same | 24 h |
| 3 | Verification of a new address | `PATCH /api/v1/admin/users/{user_id}` when an admin changes a user's email (`services/auth-service/domains/admin/service.py`, `AdminService.update_user`) | sets `is_verified = False` and `self.email_dispatch` (only when the same request does not also set `is_verified` itself); the route adds `send_verification_email_safely` | same | 24 h |
| 4 | Password reset | `POST /api/v1/auth/password-reset/request` | `AuthService.request_password_reset` (returns `None` for an unknown or deleted account) | `{FRONTEND_BASE_URL}/reset-password?token=...` | `PASSWORD_RESET_TOKEN_EXPIRE_HOURS` = 1 h |
| 5 | Organization invitation | `POST /api/v1/organizations/{org_id}/invitations` | `OrganizationService.invite` (`services/auth-service/domains/organization/service.py`) appends to `pending_invitation_emails`; the route adds `send_quietly` with `send_organization_invitation_email` for each. A new invitation to the same address in the same organization replaces the open one, so only the newest email's link works | `{FRONTEND_BASE_URL}/invitations/accept?token=...` | `ORGANIZATION_INVITATION_EXPIRE_DAYS` = 7 days (not stated in the email); ends earlier when revoked (`DELETE /api/v1/organizations/{org_id}/invitations/{id}`) or replaced |
| 6 | Notification copy | any core-service notification (`services/core-service/domains/notification/service.py`, `notify_user`) | `send_email_if_enabled` in `services/core-service/domains/notification/service.py` | none | - |

A user changing their own profile through `PATCH /api/v1/auth/me` cannot
change their email (`MeUpdate` has no email field), so that route sends
nothing.

The links point at pages of the web frontend (`/verify-email`,
`/reset-password`, `/invitations/accept`, in `frontend/app/(public)/`).
Each page reads `token` from the link and posts it to the API:
`POST /api/v1/auth/verify-email {"token"}`,
`POST /api/v1/auth/password-reset/confirm {"token", "new_password"}`,
`POST /api/v1/organizations/invitations/accept {"token"}`. The invitation
page needs a logged-in user, so it sends a visitor to `/login` first and
back afterwards. `/verify-email` also has a resend form
(`POST /api/v1/auth/verify-email/resend`), and the login page offers it
when a login fails with `email_not_verified`. You can still finish each
flow without the frontend by posting the token yourself. The token is
always posted in the body, never put into an API path, so it does not
reach access or error logs (chapter 29, section 3.1).

### 3.3 The tokens inside the links

All three link tokens are JWTs made by `services/auth-service/security.py`
(`_create_token`), signed with `JWT_SECRET`, each with its own `type`
claim. `decode_special_token(token, expected_type)` refuses a token of the
wrong type, an expired one, or one whose `sub` is not a UUID.

- **Verification** (`create_email_verification_token`): `sub` = user id,
  plus an `email` claim. `verify_email` checks that the account's current
  email still equals that claim, so an old link cannot verify a newer
  address.
- **Password reset** (`create_password_reset_token`): `sub` = user id,
  plus `pwd` = the first 32 hex characters of SHA-256 of the current
  password hash (`password_fingerprint`). When the password changes, the
  fingerprint changes, so the link works **once**. A successful reset also
  revokes all of the user's sessions (`revoke_all_sessions`).
- **Invitation** (`create_organization_invitation_token`): `sub` = the
  invitation row id (the invitee may not have an account yet).

### 3.4 Why the email is sent after the commit, in a background task

Every auth route that sends email declares its database session as
`Depends(get_db, scope="function")`. `shared/database.py` (`make_get_db`)
commits the session in the dependency's teardown. With
`scope="function"`, that teardown runs as soon as the route function
returns, **before** the response is sent. FastAPI runs `BackgroundTasks`
**after** the response is sent. So the order is always:

```
1. route runs; the service flushes rows and returns an EmailDispatch
2. route registers background_tasks.add_task(send_..._safely, dispatch)
3. route returns  -> get_db teardown -> COMMIT
      (if the commit fails: 500, and the background task never runs)
4. response sent to the client
5. background task: send_..._safely -> shared/email.py -> SMTP
```

Sending from inside the service method, before the commit, causes three
real problems (all three were seen in this project):

- an SMTP failure turns every registration into a 500 error;
- a hanging SMTP server holds the database connection open for up to the
  SMTP timeout (60 seconds);
- if the commit fails after the email was sent, the user receives a link
  to an account (or invitation) that does not exist, because the row was
  rolled back.

Sending after the commit removes all three. The price is that an email
can be lost (step 5 fails) while the row exists. That is acceptable
because each email can be requested again (resend verification, request a
new reset, invite again).

The reset and resend routes have one more reason. They must not reveal
whether an address has an account (account enumeration). They return the
same message either way ("If an account exists for this email, a reset
link has been sent."), and because the slow SMTP work happens after the
response, the response time is the same too.

### 3.5 Failure handling

Each background function catches every exception and logs a warning
(the auth-service ones through `shared.email.send_quietly`):

| Function | File | Log event on failure |
|---|---|---|
| `send_verification_email_safely` | `services/auth-service/domains/auth/service.py` | `verification_email_failed` |
| `send_password_reset_email_safely` | same | `password_reset_email_failed` |
| `send_quietly(..., send_organization_invitation_email, ...)` | `services/auth-service/domains/organization/api.py` | `invitation_email_failed` |
| `send_email_if_enabled` | `services/core-service/domains/notification/service.py` | `notification_email_send_failed` |

Inside, `shared/email.py` has already logged `email_send_failed` with the
SMTP error. There is **no retry and no outbox table**: a failed email is
gone. The user-facing response is never affected.

### 3.6 Notification emails (core-service)

core-service creates an in-app notification for most user actions
(chapter 12 and `services/core-service/domains/notification/service.py`).
For each notification it asks auth-service for the user's channels
(`GET /internal/users/{id}/notification-settings`, which returns the
account email and `user_settings.notification_preferences`):

- `in_app` (default on): write the notification row.
- `websocket` (default on): push it live.
- `email` (default **off**, opt-in): also send `send_notification_email`.

`notify_user` writes and commits the row in its own session first, then
calls `deliver_notification`, which pushes and emails. If `in_app` is off,
no row is written but the email still follows its own switch. If
auth-service cannot be reached, the defaults are used, which means no
email. Routes call `notify_after_commit`, which schedules `notify_users`
as a background task, so this also happens after the route's commit.

A user turns the email channel on with
`PATCH /api/v1/auth/me` and the body
`{"notification_preferences": {"in_app": true, "websocket": true, "email": true}}`.

### 3.7 Alert emails (Alertmanager)

Monitoring alerts do not use `shared/email.py`. Prometheus sends firing
alerts to Alertmanager, which emails them itself using
`infra/alertmanager/alertmanager.yml`:

- `smtp_smarthost: "mailhog:1025"`, `smtp_require_tls: false`,
  `smtp_from: "AlphaMind Alertmanager <alertmanager@alphamind.local>"`;
- one receiver, `to: "alerts@alphamind.local"`, `send_resolved: true`;
- grouped by `alertname` and `service`, `group_wait: 30s`,
  `group_interval: 5m`, `repeat_interval: 4h`.

Alertmanager does not expand environment variables in its config file, so
the `SMTP_*` settings do not apply to it. To send alerts to a real inbox
you edit that file (see chapter 27).

### 3.8 How tests handle email

No test talks to a real SMTP server. The auth-service test
configuration (`tests/auth-service/conftest.py`) sets
`SMTP_HOST=localhost` and `SMTP_PORT=1` (nothing listens there). The
fixture `mock_email` in the same file replaces `shared.email.send_email`
with a function that appends `{"to", "subject", "html_body"}` to a list.
A test can then read the link out of the captured HTML. Another
auth-service test
(`tests/auth-service/test_accounts.py::TestRegistration::test_succeeds_when_smtp_is_down`)
replaces
`send_email` with one that raises `EmailSendError("smtp down")`. It checks that
registration still answers 201.

---

## 4. Configuration

All `SMTP_*` settings are defined once in `shared/config.py`
(`BaseServiceSettings`), so every service has them; only auth-service and
core-service use them. The values in `.env.example` match the defaults.

| Setting | Default | Meaning | File |
|---|---|---|---|
| `SMTP_HOST` | `mailhog` | SMTP server host name (the MailHog container in development). | `shared/config.py`, `.env.example` |
| `SMTP_PORT` | `1025` | SMTP port. MailHog listens on 1025 inside the Docker network. | same |
| `SMTP_USER` | `""` | Login name; empty = no login. | same |
| `SMTP_PASSWORD` | `""` | Login password or provider SMTP key; empty = no login. | same |
| `SMTP_FROM_EMAIL` | `noreply@alphamind.local` | The `From` address. A real provider requires a sender address it has verified. | same |
| `SMTP_USE_TLS` | `false` | Use STARTTLS after connecting. Must be `true` with a real provider. | same |
| `FRONTEND_BASE_URL` | `http://localhost:3000` | Base of every link in an email. | `shared/config.py`, `.env.example` |
| `EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS` | `24` | Lifetime of a verification link. | `services/auth-service/config.py` |
| `PASSWORD_RESET_TOKEN_EXPIRE_HOURS` | `1` | Lifetime of a reset link. | `services/auth-service/config.py` |
| `ORGANIZATION_INVITATION_EXPIRE_DAYS` | `7` | Lifetime of an invitation. | `services/auth-service/config.py` |
| `RATE_LIMIT_PASSWORD_RESET_PER_EMAIL` / `_PER_IP` | `3` / `10` per `RATE_LIMIT_PASSWORD_RESET_WINDOW_SECONDS` (3600) | Limits on reset requests. | `services/auth-service/config.py` |
| `RATE_LIMIT_RESEND_VERIFICATION_PER_EMAIL` | `3` per 3600 s | Limit on resend requests. | same |
| `RATE_LIMIT_REGISTER_PER_IP` | `10` per 3600 s | Limit on registrations. | same |
| `RATE_LIMIT_INVITATIONS_PER_ORG` | `20` per 3600 s | Limit on invitations per organization. | same |
| MailHog image / ports | `mailhog/mailhog:v1.0.1`, `127.0.0.1:8025:8025` | Web UI and API on the loopback interface only. The SMTP port 1025 is not published to the host. | `docker-compose.yaml` |
| Alert email settings | see 3.7 | Alertmanager's own SMTP and recipient. | `infra/alertmanager/alertmanager.yml` |

### Production: a real provider

Nothing in the code changes. You change the env file of the deployment
(`.env.docker` on that server, never committed). Example for Brevo's
SMTP relay (take the exact host, port and login from your Brevo account
page; these are the values Brevo documents at the time of writing):

```
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USER=<your Brevo SMTP login>
SMTP_PASSWORD=<your Brevo SMTP key>
SMTP_FROM_EMAIL=<a sender address verified in Brevo>
SMTP_USE_TLS=true
FRONTEND_BASE_URL=https://<your real frontend domain>
```

Then recreate auth-service and core-service so they read the new values:
`make rebuild S="auth-service core-service"` (it runs `up -d --build`).
Do not use `RESTART_ONLY=1` here: a plain `docker compose restart` keeps
the container's old environment and does not re-read the env file.
Three things to remember:

- Use port 587 with STARTTLS. The code only supports STARTTLS
  (`start_tls`), not "implicit TLS" on port 465.
- Update `infra/alertmanager/alertmanager.yml` separately. In its
  `global` block set `smtp_smarthost`, `smtp_auth_username`,
  `smtp_auth_password` and `smtp_require_tls: true`; under the receiver
  set a real `to` address. Then restart the container:
  `docker compose --env-file .env.docker restart alertmanager`.
- Set `FRONTEND_BASE_URL` to the public frontend, or every link in every
  email points at `localhost`.

---

## 5. Design decisions and why

**Plain SMTP through `aiosmtplib`, no provider SDK.** The alternative is
a vendor library (for example a Brevo or SendGrid HTTP API client). SMTP
was chosen because every provider supports it, so changing provider is a
change of six settings, and development can use MailHog with the same
code path as production.

**Send after the commit, in a background task.** The alternative is to
send inside the request. Section 3.4 explains the three real failures
that sending inside the request causes (500 on SMTP failure, a database
connection held during a slow SMTP call, a link to a rolled-back row). The trade-off is that an email can be lost; every email
can be requested again.

**Best effort, no retry queue.** The alternative is an outbox table and a
worker that retries. For a handful of transactional emails with a "send
again" button, that machinery is not worth it. A lost notification copy
does not matter either, because the in-app notification is the record.

**Escape everything in the HTML and clean the subject.** Without escaping,
anyone who can name an organization could put a link or markup into an
invitation email sent from AlphaMind's own address, which is a phishing
risk; a CR/LF in a subject could add mail headers. So every value goes
through `html.escape` and every subject through `_clean_header`.

**Templates take finished links.** `shared/email.py` would otherwise need
to know each service's settings and URL layout. The caller owns the link,
the shared module owns the formatting.

**Notification emails are opt-in.** Email leaves the application and can
surprise people, so the `email` channel defaults to off
(`DeliveryPreferences`).

**MailHog only on the loopback interface.** Its UI and API show every
email, including reset links. Binding to `127.0.0.1:8025` keeps them
private to your machine.

---

## 6. When things go wrong

| Symptom | What the system does | How to diagnose |
|---|---|---|
| Registration answers 201 but no email arrives | The user row is committed; the background send failed. | `make service-logs S=auth-service` and look for `email_send_failed` and `verification_email_failed`. Is `mailhog` running (`make health`)? Is `SMTP_HOST` right? The user can use the resend form on `/verify-email` (or call `POST /api/v1/auth/verify-email/resend`). |
| Reset request answers 200 but nothing arrives | Same answer for unknown accounts, by design. | Check the address has an account; check for `password_reset_email_failed`; check the per-email limit (3 per hour). |
| A notification email never arrives | Email is opt-in. | Check the user's `notification_preferences` (`bash scripts/psql.sh -c "SELECT notification_preferences FROM auth.user_settings WHERE user_id = '<id>'"`); look for `notification_email_send_failed` in core-service logs. |
| `email_send_failed ... Connection refused` | SMTP server not reachable. | `docker compose --env-file .env.docker ps mailhog`; in production check host and port. |
| Authentication or TLS errors from a provider | Wrong `SMTP_USER`/`SMTP_PASSWORD`, or `SMTP_USE_TLS` off. | The error text in `email_send_failed`. |
| The link in the email goes to `localhost:3000` | `FRONTEND_BASE_URL` was not set for the deployment. | Check `.env.docker`. |
| "This reset link has already been used or is no longer valid." (422) | The password changed since the link was issued (the `pwd` fingerprint differs). | Request a new link. |
| MailHog is empty after a restart | MailHog keeps messages in memory (no volume in `docker-compose.yaml`). | Expected. |
| A 429 on reset or resend | Rate limit hit (`rate_limited`, with `Retry-After`). | Wait for the window; see chapter 29. |

Note: `email_sent` and `email_send_failed` log the recipient address.
Keep that in mind when you share log files.

---

## 7. Demo walkthrough

Goal: show that a real action produces a real email, and that the email
arrives through plain SMTP into MailHog.

1. **Open MailHog.** In a browser open `http://127.0.0.1:8025`. Say:
   "This is a fake mail server. Every email the stack sends in
   development lands here. Nothing leaves the machine."

2. **Trigger a password reset** for an account that exists (use your own
   test account's address). This writes nothing to the database; it only
   sends an email. It counts towards the limit of 3 reset requests per
   address per hour.

   ```bash
   curl -sk -X POST https://localhost:8443/api/v1/auth/password-reset/request \
     -H 'Content-Type: application/json' \
     -d '{"email": "you@example.com"}'
   ```

   Expected answer:
   `{"data":{"message":"If an account exists for this email, a reset link has been sent."},"request_id":"<id>"}`.
   `request_id` is the same id as the `X-Request-ID` response header
   (`curl -sk -D - ...` shows it) and the auth-service log lines of this
   request; error bodies carry it too. Say: "The answer is the same for an unknown address, so nobody can
   probe which emails have accounts."

3. **Show the same answer for an unknown address**, then show that only
   one email arrived:

   ```bash
   curl -sk -X POST https://localhost:8443/api/v1/auth/password-reset/request \
     -H 'Content-Type: application/json' \
     -d '{"email": "nobody-here@example.com"}'
   ```

4. **Read it in the web UI.** Refresh MailHog. Open the email "Reset your
   AlphaMind password". Show the HTML view (the link) and the source view
   (multipart/alternative with a text part and an HTML part, From
   `noreply@alphamind.local`).

5. **Read it through the MailHog API.** This is the same API the
   end-to-end check (`services/core-service/scripts/e2e_check.py`,
   `mail_token`) uses to pick up verification and invitation links.

   ```bash
   curl -s http://127.0.0.1:8025/api/v2/messages | python3 -c '
   import json, sys
   data = json.load(sys.stdin)
   print("total:", data["total"])
   for item in data["items"][:5]:
       h = item["Content"]["Headers"]
       print(item["Created"], h["To"][0], "|", h["Subject"][0])
   '
   ```

   Then extract the link from the newest email to one address
   (`/api/v2/search` filters by recipient):

   ```bash
   curl -s 'http://127.0.0.1:8025/api/v2/search?kind=to&query=you@example.com&limit=1' \
   | python3 -c '
   import email, json, re, sys
   item = json.load(sys.stdin)["items"][0]
   msg = email.message_from_string(item["Raw"]["Data"])
   for part in msg.walk():
       if part.get_content_type() == "text/html":
           html = part.get_payload(decode=True).decode()
           print(re.findall(r"href=\"([^\"]+)\"", html))
   '
   ```

   You see `['http://localhost:3000/reset-password?token=eyJ...']`. Say:
   "The token is a signed JWT of type `password_reset`. It contains a
   fingerprint of the current password hash, so it works only once."

6. **Optional, writes data (changes the account's password and logs it
   out everywhere).** Only with a throwaway account:

   ```bash
   curl -sk -X POST https://localhost:8443/api/v1/auth/password-reset/confirm \
     -H 'Content-Type: application/json' \
     -d '{"token": "<token from step 5>", "new_password": "a-new-long-password"}'
   ```

   Run the same command again: the answer is 422 "This reset link has
   already been used or is no longer valid."

7. **Optional: registration** (writes a new user row). Register a new
   address through `POST /api/v1/auth/register` and show the "Verify your
   AlphaMind account" email in MailHog.

8. **Show the code path.** Open `services/auth-service/domains/auth/api.py`
   at `request_password_reset` and point at
   `background_tasks.add_task(send_password_reset_email_safely, dispatch)`
   and at `Depends(get_db, scope="function")`. Say: "Commit first, then
   the response, then the email. If SMTP is down the user still gets the
   same answer and the failure is in the log."

Do not use `DELETE /api/v1/messages` on MailHog during a demo: it deletes
every stored email.

---

## 8. Check your understanding

<details>
<summary>1. Which services send application email, and which one never does?</summary>

auth-service (verification, password reset, organization invitation) and
core-service (optional notification copies). ai-service never sends
email. Alert emails come from Alertmanager, not from a service.
</details>

<details>
<summary>2. Why is the verification email sent in a background task and not inside <code>AuthService.register</code>?</summary>

So it runs after the commit and after the response. Sending inside the
request would turn SMTP failures into 500 errors, hold the database
connection during slow SMTP calls, and could mail a link to a user row
that is then rolled back.
</details>

<details>
<summary>3. What guarantees that the commit happens before the background task?</summary>

The routes use `Depends(get_db, scope="function")`. The teardown of
`get_db` (which commits) then runs when the route function returns,
before the response; FastAPI runs background tasks only after the
response.
</details>

<details>
<summary>4. What happens if the SMTP server is down when someone registers?</summary>

The registration succeeds (201). `email_send_failed` and
`verification_email_failed` are logged. The user asks for a new link with
`POST /api/v1/auth/verify-email/resend`.
</details>

<details>
<summary>5. Why can a password reset link be used only once?</summary>

The token carries `pwd`, a fingerprint of the password hash at issue
time. After a reset the hash changes, so `confirm_password_reset` refuses
the old token.
</details>

<details>
<summary>6. What do you change to use Brevo in production?</summary>

Only settings: `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`,
`SMTP_PASSWORD`, `SMTP_FROM_EMAIL` (a verified sender),
`SMTP_USE_TLS=true`, plus `FRONTEND_BASE_URL`; and, separately, the
Alertmanager SMTP block in `infra/alertmanager/alertmanager.yml`.
</details>

<details>
<summary>7. Why are reset requests answered with the same message for unknown addresses?</summary>

To prevent account enumeration. Sending in the background also makes the
response time the same.
</details>

<details>
<summary>8. Does a user get notification emails by default?</summary>

No. The `email` channel is opt-in (default off in `DeliveryPreferences`).
</details>

---

## 9. Code map

| File | What it holds |
|---|---|
| `shared/email.py` | `send_email`, `send_quietly`, `EmailSendError`, escaping helpers, the four template helpers. |
| `shared/config.py` | `SMTP_*` settings and `FRONTEND_BASE_URL`. |
| `services/auth-service/domains/auth/service.py` | `EmailDispatch`, `send_verification_email_safely`, `send_password_reset_email_safely`, `register`, `resend_verification`, `request_password_reset`, `confirm_password_reset`. |
| `services/auth-service/domains/auth/api.py` | The routes that schedule those emails as background tasks. |
| `services/auth-service/domains/admin/service.py`, `api.py` | Admin email change -> new verification email. |
| `services/auth-service/domains/organization/service.py`, `api.py` | `invite`, `pending_invitation_emails` (the route sends each with `send_quietly`). |
| `services/auth-service/security.py` | The single-purpose link tokens. |
| `services/auth-service/config.py` | Link lifetimes and rate limits. |
| `services/core-service/domains/notification/service.py` | `DeliveryPreferences`, `delivery_preferences`, `send_email_if_enabled`, `deliver_notification`, `notify_user`, `notify_users`, `notify_after_commit`. |
| `infra/alertmanager/alertmanager.yml` | Alert email routing and SMTP. |
| `docker-compose.yaml` (`mailhog`) | The MailHog container. |
| `tests/auth-service/conftest.py` (`mock_email`) | Captures emails in tests. |
| `services/core-service/scripts/e2e_check.py` (`mail_token`, `token_from_mailhog`) | Reads links from MailHog during the end-to-end check. |

---

## 10. Related chapters

- [11-auth-service.md](11-auth-service.md): registration, login and the
  account flows that send these emails.
- [12-core-service.md](12-core-service.md): the notification domain.
- [27-observability.md](27-observability.md): Prometheus rules and
  Alertmanager.
- [29-security.md](29-security.md): JWTs, rate limits, enumeration.
- [30-shared-library.md](30-shared-library.md): `shared/email.py` among
  the other shared modules.
- [../API_SPEC.md](../API_SPEC.md): request and response shapes of the
  routes above.
