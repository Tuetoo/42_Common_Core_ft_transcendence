import argparse
import asyncio
import contextlib
import csv
import email
import email.policy
import functools
import html
import io
import json
import os
import re
import secrets
import ssl
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage
from typing import Any, TextIO, TypeVar
from uuid import UUID, uuid4
import httpx

OK = "OK"
FAIL = "FAIL"
SKIP = "SKIP"

TICKER = "AMD"
DEFAULT_BASE_URL = "https://gateway"
DEFAULT_MAILHOG_URL = "http://mailhog:8025"
DEFAULT_HOST_HEADER = "localhost"
TOKEN_REFRESH_AFTER_SECONDS = 600.0
DEFAULT_CHECK_TIMEOUT = 60.0
DOCUMENT_READY_TIMEOUT = 180.0
ANALYSIS_TIMEOUT = 15 * 60.0
YJS_SNAPSHOT_DEBOUNCE_SECONDS = 30.0

SECTIONS: tuple[str, ...] = (
    "platform", "auth", "roles", "org", "portfolio", "apikey", "documents",
    "research", "realtime", "yjs", "analytics", "backtest", "analysis",
    "admin", "cleanup")
SECTION_DEPS: dict[str, tuple[str, ...]] = {
    "platform": (),
    "auth": (),
    "roles": ("auth", ),
    "org": ("auth", ),
    "portfolio": ("auth", ),
    "apikey": ("auth", "portfolio"),
    "documents": ("auth", ),
    "research": ("auth", "org"),
    "realtime": ("auth", ),
    "yjs": ("auth", "org"),
    "analytics": ("auth", ),
    "backtest": ("auth", "portfolio"),
    "analysis": ("auth", "portfolio"),
    "admin": ("auth", ),
    "cleanup": ()
}

ADMIN_TOKEN_HELP = """\
E2E_ADMIN_TOKEN: an access token of your admin account. Make a 2-hour one
with auth-service's own helper (replace <ADMIN_USER_ID> with the id of the
admin account, e.g. from GET /api/v1/auth/me after logging in):

  TOKEN=$(docker compose --env-file .env.docker exec -T auth-service \\
    python -c "from datetime import timedelta; from security import \\
_create_token; print(_create_token('<ADMIN_USER_ID>', timedelta(hours=2), \\
'access', {'role': 'admin'}))")

Then run inside the core-service container:

  docker compose --env-file .env.docker exec -T -e E2E_ADMIN_TOKEN="$TOKEN" \\
    core-service python -m scripts.e2e_check [--with-analysis] [--keep] \\
    [--only SECTION[,SECTION]] [--verbose]

Sections: """ + ", ".join(SECTIONS)

T = TypeVar("T")


class CheckFailed(Exception):
    pass


class SkipCheck(Exception):
    pass


_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_SECRET_FIELD_RE = re.compile(
    r'("(?:access_token|refresh_token|token|key|password|new_password|'
    r'challenge_token|secret)"\s*:\s*)"[^"]*"')


def redact(text: str, secret_values: Iterable[str] = ()) -> str:
    result = _JWT_RE.sub("<redacted-jwt>", text)
    result = _SECRET_FIELD_RE.sub(r'\1"<redacted>"', result)
    for value in secret_values:
        if value and len(value) >= 6:
            result = result.replace(value, "<redacted>")
    return result


def one_line(text: str, limit: int = 300) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def resolve_sections(
        only: Sequence[str] | None) -> tuple[list[str], list[str]]:
    if not only:
        return list(SECTIONS), []
    unknown = [name for name in only if name not in SECTIONS]
    if unknown:
        raise ValueError(
            f"unknown section(s): {', '.join(unknown)}; known: "
            f"{', '.join(SECTIONS)}")
    wanted: set[str] = set()

    def add(name: str) -> None:
        if name in wanted:
            return
        wanted.add(name)
        for dep in SECTION_DEPS[name]:
            add(dep)

    for name in only:
        add(name)
    wanted.add("cleanup")
    added = [
        n for n in SECTIONS if n in wanted and n not in only and n != "cleanup"
    ]
    return [n for n in SECTIONS if n in wanted], added


def _message_texts(raw: str) -> list[str]:
    message = email.message_from_string(raw, policy=email.policy.default)
    texts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        content: Any = None
        if isinstance(part, EmailMessage):
            with contextlib.suppress(Exception):
                content = part.get_content()
        if content is None:
            payload = part.get_payload(decode=True)
            content = (
                payload.decode("utf-8", "replace") if isinstance(
                    payload, bytes) else str(payload or ""))
        texts.append(str(content))
    return texts


def extract_link_token(raw_message: str, path: str) -> str | None:
    pattern = re.compile(re.escape(path) + r"\?token=([A-Za-z0-9_.\-]+)")
    for text in _message_texts(raw_message):
        match = pattern.search(html.unescape(text))
        if match:
            return match.group(1)
    return None


def token_from_mailhog(
        search_result: Mapping[str, Any], path: str) -> str | None:
    items = list(search_result.get("items") or [])
    items.sort(key=lambda item: str(item.get("Created") or ""), reverse=True)
    for item in items:
        raw = (item.get("Raw") or {}).get("Data")
        if not raw:
            content = item.get("Content") or {}
            headers = content.get("Headers") or {}
            header_text = "".join(
                f"{name}: {', '.join(values)}\r\n"
                for name, values in headers.items())
            raw = header_text + "\r\n" + str(content.get("Body") or "")
        token = extract_link_token(str(raw), path)
        if token:
            return token
    return None


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(lines: Sequence[str]) -> bytes:
    safe = [
        "".join(ch for ch in line if 32 <= ord(ch) < 127) for line in lines
    ]
    ops = ["BT", "/F1 11 Tf", "14 TL", "72 740 Td"]
    for index, line in enumerate(safe):
        if index:
            ops.append("T*")
        ops.append(f"({_pdf_escape(line)}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>", b"<< /Length " +
        str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []
    header = rows[0]
    return header, [
        dict(zip(header, row, strict=False)) for row in rows[1:] if row
    ]


def fmt_decimal(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _ledger_phase(row: Mapping[str, Any]) -> int:
    kind = str(row.get("transaction_type"))
    if kind == "sell" or (kind == "adjustment" and
                          (_dec(row.get("quantity")) or Decimal(0)) < 0):
        return 2
    if kind == "stock_split":
        return 1
    return 0


def replay_ledger(
    transactions: Iterable[Mapping[str, Any]]
) -> tuple[dict[str, tuple[Decimal, Decimal]], Decimal]:
    rows = sorted(
        transactions, key=lambda t:
        (str(t.get("transaction_date")), _ledger_phase(t)))
    holdings: dict[str, tuple[Decimal, Decimal]] = {}
    cash = Decimal(0)
    for row in rows:
        kind = str(row.get("transaction_type"))
        ticker = str(row.get("ticker"))
        qty = _dec(row.get("quantity")) or Decimal(0)
        price = _dec(row.get("price"))
        held_qty, held_cost = holdings.get(ticker, (Decimal(0), Decimal(0)))
        if kind == "deposit":
            cash += qty
        elif kind == "withdrawal":
            cash -= qty
        elif kind == "dividend":
            cash += qty * (_dec(row.get("dividend_per_share")) or Decimal(0))
        elif kind == "buy" and price is not None:
            cash -= qty * price
            new_qty = held_qty + qty
            holdings[ticker] = (
                new_qty, (held_qty * held_cost + qty * price) / new_qty)
        elif kind == "sell" and price is not None:
            cash += qty * price
            holdings[ticker] = (held_qty - qty, held_cost)
        elif kind == "stock_split":
            ratio = _dec(row.get("split_ratio")) or Decimal(1)
            holdings[ticker] = (held_qty * ratio, held_cost / ratio)
        elif kind == "adjustment":
            if qty > 0:
                unit = price if price is not None else Decimal(0)
                new_qty = held_qty + qty
                holdings[ticker] = (
                    new_qty, (held_qty * held_cost + qty * unit) / new_qty)
            else:
                holdings[ticker] = (held_qty + qty, held_cost)
    return {t: v for t, v in holdings.items() if v[0] != 0}, cash


class YClient:

    def __init__(self) -> None:
        import y_py as Y

        self._y = Y
        self.doc = Y.YDoc()
        self.text = self.doc.get_text("content")

    def apply(self, update: bytes) -> None:
        self._y.apply_update(self.doc, update)

    def value(self) -> str:
        return str(self.text)

    def append(self, chunk: str) -> bytes:
        before = self._y.encode_state_vector(self.doc)
        length = len(self.text)
        self.doc.transact(lambda txn: self.text.insert(txn, length, chunk))
        return bytes(self._y.encode_state_as_update(self.doc, before))


def format_line(status: str, name: str, detail: str) -> str:
    return f"{status:<4} {name}  {detail}".rstrip()


@dataclass
class Result:
    name: str
    status: str
    detail: str
    seconds: float


@dataclass
class Actor:
    name: str
    email: str
    username: str
    password: str
    user_id: str | None = None
    access: str | None = None
    refresh: str | None = None
    issued_at: float = 0.0
    deleted: bool = False


@dataclass
class Created:
    kind: str
    owner: str
    id: str
    label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class ADMIN:
    pass


Who = Actor | type[ADMIN] | None


def _need(value: T | None, what: str) -> T:
    if value is None:
        raise SkipCheck(f"needs {what}")
    return value


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _data(resp: httpx.Response) -> Any:
    body = _json(resp)
    return body.get("data") if isinstance(body, dict) else None


class E2E:

    def __init__(self, args: argparse.Namespace, out: TextIO) -> None:
        self.args = args
        self.out = out
        self.run_id = args.run_id or secrets.token_hex(3)
        self.prefix = f"E2E {self.run_id}"
        self.admin_token: str = os.environ.get("E2E_ADMIN_TOKEN", "").strip()
        self.results: dict[str, Result] = {}
        self.order: list[str] = []
        self.secrets: list[str] = [self.admin_token]
        self.created: list[Created] = []
        self.analysis_ids: list[str] = []
        self.users: dict[str, Actor] = {}
        for name in ("alice", "bob", "gina"):
            password = secrets.token_urlsafe(18)
            self.secrets.append(password)
            self.users[name] = Actor(
                name=name, email=f"e2e-{self.run_id}-{name}@example.com",
                username=f"e2e_{self.run_id}_{name}", password=password)
        self.admin_id: str | None = None
        self.stats_baseline: dict[str, Any] | None = None
        self.org_id: str | None = None
        self.org_note_id: str | None = None
        self.portfolio_id: str | None = None
        self.holding_id: str | None = None
        self.research_note_id: str | None = None
        self.research_comment_id: str | None = None
        self.documents: dict[str, dict[str, Any]] = {}
        self.backtest_id: str | None = None
        self._db_used = False
        self._renew_lock = asyncio.Lock()
        base = args.base_url.rstrip("/")
        self.base_url = base
        self.ws_base = (
            "wss" + base[5:] if base.startswith("https") else
            ("ws" + base[4:]))
        headers = ({
            "Host": args.insecure_host_header
        } if (args.insecure_host_header) else {})
        self.api = httpx.AsyncClient(
            base_url=base, verify=False, headers=headers, timeout=30.0,
            follow_redirects=False)
        self.mail = httpx.AsyncClient(
            base_url=args.mailhog_url.rstrip("/"), timeout=10.0)

    def _print(self, line: str) -> None:
        print(redact(line, self.secrets), file=self.out, flush=True)

    def info(self, text: str) -> None:
        self._print(f"INFO {text}")

    def record(
            self, name: str, status: str, detail: str,
            seconds: float = 0.0) -> None:
        detail = one_line(redact(detail, self.secrets))
        self.results[name] = Result(name, status, detail, seconds)
        self.order.append(name)
        self._print(format_line(status, name, detail))

    def passed(self, name: str) -> bool:
        result = self.results.get(name)
        return result is not None and result.status == OK

    async def check(
            self, name: str, fn: Callable[[], Awaitable[str | None]], *,
            requires: Sequence[str] = (),
            timeout: float = DEFAULT_CHECK_TIMEOUT) -> bool:
        missing = [r for r in requires if not self.passed(r)]
        if missing:
            self.record(name, SKIP, f"needs {', '.join(missing)}")
            return False
        started = time.monotonic()
        try:
            detail = await asyncio.wait_for(fn(), timeout=timeout)
            status = OK
        except SkipCheck as exc:
            status, detail = SKIP, str(exc)
        except CheckFailed as exc:
            status, detail = FAIL, str(exc)
        except TimeoutError:
            status, detail = FAIL, f"timed out after {timeout:g}s"
        except Exception as exc:
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        self.record(name, status, detail or "", time.monotonic() - started)
        return status == OK

    def skip_all(self, names: Sequence[str], reason: str) -> None:
        for name in names:
            self.record(name, SKIP, reason)

    async def bearer(self, who: Who) -> str | None:
        if who is None:
            return None
        if who is ADMIN:
            return self.admin_token
        assert isinstance(who, Actor)
        if who.access is None:
            raise SkipCheck(f"{who.name} is not logged in")
        if time.monotonic() - who.issued_at > TOKEN_REFRESH_AFTER_SECONDS:
            async with self._renew_lock:
                if (time.monotonic() - who.issued_at
                        > TOKEN_REFRESH_AFTER_SECONDS):
                    await self.renew(who)
        return who.access

    async def req(
            self, method: str, path: str, *, who: Who = None,
            token: str | None = None, json_body: Any = None,
            params: Mapping[str, Any] | None = None, files: Any = None,
            data: Mapping[str, Any] | None = None, api_key: str | None = None,
            timeout: float = 30.0) -> httpx.Response:
        headers: dict[str, str] = {}
        bearer = token if token is not None else await self.bearer(who)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if api_key is not None:
            headers["X-API-Key"] = api_key
        started = time.monotonic()
        resp = await self.api.request(
            method, path, headers=headers, json=json_body, params=params,
            files=files, data=data, timeout=timeout)
        if self.args.verbose:
            self._print(
                f"     {method} {path} -> {resp.status_code} "
                f"({time.monotonic() - started:.2f}s)")
        return resp

    def expect(self, resp: httpx.Response, *codes: int) -> None:
        if resp.status_code not in codes:
            body = one_line(redact(resp.text, self.secrets), 220)
            wanted = "/".join(str(c) for c in codes)
            raise CheckFailed(
                f"HTTP {resp.status_code} (expected {wanted}): {body}")

    async def admin_token_problem(self) -> str | None:
        try:
            resp = await self.req("GET", "/api/v1/auth/me", who=ADMIN)
        except httpx.HTTPError as exc:
            return f"gateway unreachable ({type(exc).__name__}: {exc})"
        if resp.status_code != 200:
            return f"GET /api/v1/auth/me -> {resp.status_code}"
        role = (_data(resp) or {}).get("system_role")
        if role != "admin":
            return f"the token's user has role {role!r}, not admin"
        return None

    async def login(self, actor: Actor) -> httpx.Response:
        return await self.req(
            "POST", "/api/v1/auth/login", json_body={
                "email": actor.email,
                "password": actor.password
            })

    async def login_ok(self, actor: Actor) -> None:
        resp = await self.login(actor)
        self.expect(resp, 200)
        tokens = _data(resp)
        self._set_tokens(actor, tokens)

    def _set_tokens(self, actor: Actor, tokens: Mapping[str, Any]) -> None:
        actor.access = str(tokens["access_token"])
        actor.refresh = str(tokens["refresh_token"])
        actor.issued_at = time.monotonic()
        self.secrets.extend([actor.access, actor.refresh])

    async def renew(self, actor: Actor) -> None:
        if actor.deleted:
            raise SkipCheck(f"{actor.name} was deleted")
        if actor.refresh:
            resp = await self.api.post(
                "/api/v1/auth/refresh", json={"refresh_token": actor.refresh})
            if resp.status_code == 200:
                self._set_tokens(actor, _data(resp))
                return
        await self.login_ok(actor)

    def user(self, name: str) -> Actor:
        actor = self.users[name]
        if actor.access is None or actor.user_id is None:
            raise SkipCheck(f"needs a logged-in {name}")
        return actor

    async def mail_token(
            self, address: str, path: str, timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        while True:
            resp = await self.mail.get(
                "/api/v2/search", params={
                    "kind": "to",
                    "query": address,
                    "limit": 50
                })
            if resp.status_code == 200:
                token = token_from_mailhog(resp.json(), path)
                if token:
                    self.secrets.append(token)
                    return token
            if time.monotonic() > deadline:
                raise CheckFailed(
                    f"no email with a {path} link for {address} in MailHog "
                    f"within {timeout:g}s (last HTTP {resp.status_code})")
            await asyncio.sleep(1.0)

    async def notifications(self, who: Actor) -> list[dict[str, Any]]:
        resp = await self.req(
            "GET", "/api/v1/notifications", who=who, params={"page_size": 100})
        self.expect(resp, 200)
        items = (_json(resp) or {}).get("items") or []
        return [i for i in items if isinstance(i, dict)]

    async def wait_notification(
            self, who: Actor, ntype: str, *, contains: str | None = None,
            count: int = 1, timeout: float = 20.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        seen: list[dict[str, Any]] = []
        while True:
            seen = await self.notifications(who)
            matches = [
                n for n in seen if n.get("type") == ntype and (
                    contains is None or contains in f"{n.get('title')} "
                    f"{n.get('content')}")
            ]
            if len(matches) >= count:
                return matches
            if time.monotonic() > deadline:
                types = sorted({str(n.get("type")) for n in seen})
                raise CheckFailed(
                    f"{who.name}: {len(matches)}/{count} '{ntype}' "
                    f"notification(s)"
                    f"{f' containing {contains!r}' if contains else ''} after "
                    f"{timeout:g}s; types seen: {', '.join(types) or 'none'}")
            await asyncio.sleep(1.0)

    async def ws_open(self, path: str, token: str) -> Any:
        from websockets.asyncio.client import connect

        ctx: ssl.SSLContext | None = None
        if self.ws_base.startswith("wss"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        url = f"{self.ws_base}{path}?token={token}"
        return await connect(
            url, ssl=ctx, open_timeout=15, max_size=2**22, ping_interval=20)

    @staticmethod
    async def ws_close_code(ws: Any, timeout: float) -> int | None:
        from websockets.exceptions import ConnectionClosed

        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                await asyncio.wait_for(ws.recv(), timeout=remaining)
        except ConnectionClosed as exc:
            return exc.rcvd.code if exc.rcvd is not None else None
        except TimeoutError:
            return None

    @staticmethod
    async def ws_json_messages(ws: Any,
                               seconds: float) -> list[dict[str, Any]]:
        from websockets.exceptions import ConnectionClosed

        messages: list[dict[str, Any]] = []
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return messages
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except (TimeoutError, ConnectionClosed):
                return messages
            with contextlib.suppress(ValueError):
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    messages.append(parsed)

    def track(
            self, kind: str, owner: str, obj_id: str, label: str = "",
            **extra: Any) -> None:
        self.created.append(Created(kind, owner, obj_id, label, dict(extra)))

    def untrack(self, kind: str, obj_id: str) -> None:
        self.created = [
            c for c in self.created if not (c.kind == kind and c.id == obj_id)
        ]

    async def core_internal_get(self, path: str) -> httpx.Response:
        from config import settings
        from shared.http_client import get_http_client

        return await get_http_client(10.0).get(
            f"{settings.CORE_SERVICE_URL}{path}")

    @staticmethod
    def storage_head(key: str) -> bool:
        from botocore.exceptions import ClientError
        from config import settings
        from shared.storage import _get_client

        client = _get_client(settings)
        try:
            client.head_object(Bucket=settings.S3_BUCKET_DOCUMENTS, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    async def close(self) -> None:
        await self.api.aclose()
        await self.mail.aclose()
        with contextlib.suppress(Exception):
            from shared.http_client import close_http_clients

            await close_http_clients()
        if self._db_used:
            with contextlib.suppress(Exception):
                from database import engine

                await engine.dispose()

    async def s_platform(self) -> None:
        for service, storage in (("auth", False), ("core", True), ("ai",
                                                                   True)):

            async def health(
                    service: str = service, storage: bool = storage) -> str:
                resp = await self.req("GET", f"/health/{service}")
                self.expect(resp, 200)
                body = _json(resp) or {}
                if body.get("status") != "ok":
                    raise CheckFailed(f"status={body.get('status')!r}: {body}")
                if storage and body.get("storage") is not True:
                    raise CheckFailed(f"storage={body.get('storage')!r}")
                deps = ", ".join(
                    f"{k}={v}" for k, v in body.items() if isinstance(v, bool))
                return f"{body.get('service')}: {deps}"

            await self.check(f"platform.health_{service}", health)

        async def status_page() -> str:
            resp = await self.req("GET", "/status")
            self.expect(resp, 200)
            ctype = resp.headers.get("content-type", "")
            if "text/html" not in ctype:
                raise CheckFailed(f"content-type {ctype!r}")
            return f"{len(resp.content)} bytes of HTML"

        await self.check("platform.status_page", status_page)

    async def s_auth(self) -> None:
        from shared.permissions import sorted_permissions

        async def admin_token() -> str:
            if not self.admin_token:
                raise CheckFailed("E2E_ADMIN_TOKEN is not set")
            resp = await self.req("GET", "/api/v1/auth/me", who=ADMIN)
            self.expect(resp, 200)
            me = _data(resp)
            if me.get("system_role") != "admin":
                raise CheckFailed(
                    f"token user has role {me.get('system_role')!r}")
            if "user:manage" not in me.get("permissions", []):
                raise CheckFailed("admin /me permissions lack user:manage")
            self.admin_id = str(me["id"])
            return f"admin {me.get('username')} ({self.admin_id})"

        await self.check("auth.admin_token", admin_token)

        async def stats_baseline() -> str:
            resp = await self.req("GET", "/api/v1/admin/stats", who=ADMIN)
            self.expect(resp, 200)
            self.stats_baseline = _data(resp)
            auth = self.stats_baseline.get("auth") or {}
            parts = [f"users_total={auth.get('users_total')}"]
            for part in ("core", "ai"):
                if self.stats_baseline.get(part) is None:
                    parts.append(f"{part}=null (service unreachable)")
            return "baseline " + ", ".join(parts)

        await self.check(
            "auth.admin_stats", stats_baseline,
            requires=("auth.admin_token", ))

        for actor in self.users.values():

            async def register(actor: Actor = actor) -> str:
                resp = await self.req(
                    "POST", "/api/v1/auth/register", json_body={
                        "email": actor.email,
                        "username": actor.username,
                        "password": actor.password
                    })
                if resp.status_code == 429:
                    raise CheckFailed(
                        "429 rate limited (auth allows 10 registrations per "
                        "address per hour; wait and re-run)")
                self.expect(resp, 201)
                self.track("user", actor.name, actor.email, actor.username)
                return actor.email

            await self.check(f"auth.register_{actor.name}", register)

            async def verify(actor: Actor = actor) -> str:
                started = time.monotonic()
                token = await self.mail_token(actor.email, "/verify-email")
                resp = await self.req(
                    "POST", "/api/v1/auth/verify-email",
                    json_body={"token": token})
                self.expect(resp, 200)
                return (
                    f"link from MailHog after "
                    f"{time.monotonic() - started:.1f}s")

            await self.check(
                f"auth.verify_{actor.name}", verify,
                requires=(f"auth.register_{actor.name}", ))

            async def login(actor: Actor = actor) -> str:
                await self.login_ok(actor)
                resp = await self.req("GET", "/api/v1/auth/me", who=actor)
                self.expect(resp, 200)
                me = _data(resp)
                actor.user_id = str(me["id"])
                for created in self.created:
                    if created.kind == "user" and created.owner == actor.name:
                        created.extra["user_id"] = actor.user_id
                return f"{actor.username} id={actor.user_id}"

            await self.check(
                f"auth.login_{actor.name}", login,
                requires=(f"auth.verify_{actor.name}", ))

        async def me_permissions() -> str:
            expected = sorted_permissions("user")
            checked = []
            for actor in self.users.values():
                if actor.access is None:
                    continue
                resp = await self.req("GET", "/api/v1/auth/me", who=actor)
                self.expect(resp, 200)
                me = _data(resp)
                if me.get("permissions") != expected:
                    raise CheckFailed(
                        f"{actor.name}: {me.get('permissions')} != {expected}")
                if me.get("system_role") != "user":
                    raise CheckFailed(
                        f"{actor.name}: role {me.get('system_role')}")
                checked.append(actor.name)
            if not checked:
                raise SkipCheck("no logged-in test user")
            return f"{len(expected)} permissions for {', '.join(checked)}"

        await self.check("auth.me_permissions", me_permissions)

        async def stats_forbidden() -> str:
            resp = await self.req(
                "GET", "/api/v1/admin/stats", who=self.user("alice"))
            self.expect(resp, 403)
            return "alice gets 403"

        await self.check(
            "auth.admin_stats_forbidden_for_user", stats_forbidden,
            requires=("auth.login_alice", ))

    async def s_roles(self) -> None:
        gina = self.users["gina"]
        bob = self.users["bob"]

        async def gina_guest() -> str:
            resp = await self.req(
                "PATCH", f"/api/v1/admin/users/{_need(gina.user_id, 'gina')}",
                who=ADMIN, json_body={"role": "guest"})
            self.expect(resp, 200)
            return f"system_role={_data(resp).get('system_role')}"

        if not await self.check(
                "roles.make_gina_guest", gina_guest,
                requires=("auth.login_gina", "auth.admin_token")):
            self.skip_all((
                "roles.guest_write_403", "roles.guest_read_200",
                "roles.guest_permissions_empty"),
                          "needs roles.make_gina_guest")
        else:

            async def guest_write() -> str:
                resp = await self.req(
                    "POST", "/api/v1/portfolios", token=gina.access,
                    json_body={"name": f"{self.prefix} guest portfolio"})
                if resp.status_code == 201:
                    pid = str(_data(resp)["id"])
                    self.track("portfolio", "gina", pid)
                self.expect(resp, 403)
                return "existing token: POST /portfolios -> 403"

            await self.check("roles.guest_write_403", guest_write)

            async def guest_read() -> str:
                resp = await self.req(
                    "GET", "/api/v1/portfolios", token=gina.access)
                self.expect(resp, 200)
                return "existing token: GET /portfolios -> 200"

            await self.check("roles.guest_read_200", guest_read)

            async def guest_perms() -> str:
                resp = await self.req(
                    "GET", "/api/v1/auth/me", token=gina.access)
                self.expect(resp, 200)
                me = _data(resp)
                if (me.get("permissions") != []
                        or me.get("system_role") != "guest"):
                    raise CheckFailed(
                        f"role={me.get('system_role')} "
                        f"permissions={me.get('permissions')}")
                return "system_role=guest permissions=[]"

            await self.check("roles.guest_permissions_empty", guest_perms)

        old_token = bob.access
        deactivated = await self.check(
            "roles.deactivate_bob", self._deactivate_bob,
            requires=("auth.login_bob", "auth.admin_token"))
        try:

            async def core_401() -> str:
                resp = await self.req(
                    "GET", "/api/v1/portfolios", token=old_token)
                self.expect(resp, 401)
                return f"core: 401 {one_line(resp.text, 80)}"

            async def ai_401() -> str:
                resp = await self.req(
                    "GET", "/api/v1/analyses", token=old_token)
                self.expect(resp, 401)
                return "ai: GET /analyses -> 401"

            async def login_fails() -> str:
                resp = await self.login(bob)
                self.expect(resp, 401)
                return "login -> 401"

            dep = ("roles.deactivate_bob", )
            await self.check(
                "roles.deactivated_core_401", core_401, requires=dep)
            await self.check("roles.deactivated_ai_401", ai_401, requires=dep)
            await self.check(
                "roles.deactivated_login_fails", login_fails, requires=dep)
        finally:
            if deactivated:
                await self.check("roles.reactivate_bob", self._reactivate_bob)

        async def relogin() -> str:
            await self.login_ok(bob)
            for path in ("/api/v1/portfolios", "/api/v1/analyses"):
                resp = await self.req("GET", path, who=bob)
                self.expect(resp, 200)
            return "new login works on core and ai"

        await self.check(
            "roles.reactivated_login", relogin,
            requires=("roles.reactivate_bob", ))

    async def _deactivate_bob(self) -> str:
        bob = self.users["bob"]
        resp = await self.req(
            "POST",
            f"/api/v1/admin/users/{_need(bob.user_id, 'bob')}/deactivate",
            who=ADMIN)
        self.expect(resp, 200)
        if _data(resp).get("is_active") is not False:
            raise CheckFailed("is_active is not false after deactivate")
        return "is_active=false"

    async def _reactivate_bob(self) -> str:
        bob = self.users["bob"]
        resp = await self.req(
            "POST",
            f"/api/v1/admin/users/{_need(bob.user_id, 'bob')}/reactivate",
            who=ADMIN)
        self.expect(resp, 200)
        if _data(resp).get("is_active") is not True:
            raise CheckFailed("is_active is not true after reactivate")
        return "is_active=true"

    async def s_org(self) -> None:

        async def create() -> str:
            alice = self.user("alice")
            resp = await self.req(
                "POST", "/api/v1/organizations", who=alice,
                json_body={"name": f"{self.prefix} org"})
            self.expect(resp, 201)
            self.org_id = str(_data(resp)["id"])
            self.track("organization", "alice", self.org_id)
            return f"org {self.org_id}"

        await self.check("org.create", create, requires=("auth.login_alice", ))

        async def invite(name: str, role: str) -> str:
            invitee = self.user(name)
            resp = await self.req(
                "POST", f"/api/v1/organizations/{_need(self.org_id, 'org')}"
                "/invitations", who=self.user("alice"), json_body={
                    "email": invitee.email,
                    "role": role
                })
            self.expect(resp, 201)
            return f"{name} invited as {_data(resp).get('role')}"

        async def accept(name: str) -> str:
            invitee = self.user(name)
            token = await self.mail_token(invitee.email, "/invitations/accept")
            resp = await self.req(
                "POST", "/api/v1/organizations/invitations/accept",
                who=invitee, json_body={"token": token})
            self.expect(resp, 200)
            return f"{name} joined as {_data(resp).get('role')}"

        for name in ("bob", "gina"):
            await self.check(
                f"org.invite_{name}_viewer",
                functools.partial(invite, name, "viewer"),
                requires=("org.create", f"auth.login_{name}"))
            await self.check(
                f"org.{name}_accepts", functools.partial(accept, name),
                requires=(f"org.invite_{name}_viewer", ))

        async def org_note() -> str:
            resp = await self.req(
                "POST", "/api/v1/research/notes", who=self.user("alice"),
                json_body={
                    "title": f"{self.prefix} org note",
                    "content": f"{self.prefix} shared body.",
                    "organization_id": self.org_id,
                    "tags": [TICKER]
                })
            self.expect(resp, 201)
            self.org_note_id = str(_data(resp)["id"])
            self.track("note", "alice", self.org_note_id)
            return f"note {self.org_note_id}"

        await self.check(
            "org.note_create", org_note, requires=("org.create", ))

        note_path = f"/api/v1/research/notes/{self.org_note_id}"

        async def viewer_read() -> str:

            async def attempt() -> dict[str, Any] | None:
                resp = await self.req("GET", note_path, who=self.user("bob"))
                return _data(resp) if resp.status_code == 200 else None

            note = await poll(attempt, 20.0, 1.0)
            if note is None:
                raise CheckFailed(
                    "bob cannot read the organization note (20s)")
            if note.get("can_edit") is not False:
                raise CheckFailed(
                    f"can_edit={note.get('can_edit')!r} for a viewer")
            return "200, can_edit=false"

        viewer = ("org.note_create", "org.bob_accepts")
        await self.check("org.viewer_reads_note", viewer_read, requires=viewer)

        async def viewer_edit() -> str:
            resp = await self.req(
                "PATCH", note_path, who=self.user("bob"),
                json_body={"title": f"{self.prefix} bob edit"})
            self.expect(resp, 403)
            return "PATCH -> 403"

        await self.check(
            "org.viewer_edit_403", viewer_edit,
            requires=("org.viewer_reads_note", ))

        async def viewer_comment() -> str:
            resp = await self.req(
                "POST", f"{note_path}/comments", who=self.user("bob"),
                json_body={"content": "viewer comment"})
            self.expect(resp, 403)
            return "POST comment -> 403"

        await self.check(
            "org.viewer_comment_403", viewer_comment,
            requires=("org.viewer_reads_note", ))

        async def to_member() -> str:
            resp = await self.req(
                "PATCH", f"/api/v1/organizations/{self.org_id}/members/"
                f"{self.user('bob').user_id}/role", who=self.user("alice"),
                json_body={"role": "member"})
            self.expect(resp, 200)
            return f"bob role={_data(resp).get('role')}"

        await self.check(
            "org.bob_to_member", to_member, requires=("org.bob_accepts", ))

        async def member_comment() -> str:
            started = time.monotonic()

            async def attempt() -> httpx.Response | None:
                resp = await self.req(
                    "POST", f"{note_path}/comments", who=self.user("bob"),
                    json_body={"content": f"{self.prefix} member comment"})
                return resp if resp.status_code == 201 else None

            resp = await poll(attempt, 25.0, 2.0)
            if resp is None:
                raise CheckFailed(
                    "bob still cannot comment 25s after the role change")
            return (
                f"201 after {time.monotonic() - started:.1f}s (role cache <= "
                f"15s)")

        await self.check(
            "org.member_can_comment", member_comment,
            requires=("org.bob_to_member", "org.note_create"))

        async def notes_ok() -> str:
            alice, bob = self.user("alice"), self.user("bob")
            org_name = f"{self.prefix} org"
            await self.wait_notification(
                alice, "organization_created", contains=org_name)
            await self.wait_notification(
                bob, "organization_invite", contains=org_name)
            await self.wait_notification(
                bob, "organization_member_joined", contains=org_name)
            await self.wait_notification(
                alice, "organization_member_joined", contains=org_name,
                count=2)
            await self.wait_notification(
                bob, "organization_role_changed", contains="member")
            await self.wait_notification(
                alice, "organization_role_changed", contains="member")
            return (
                "alice: created, 2x member_joined, role_changed; bob: invite, "
                "member_joined, role_changed")

        await self.check(
            "org.notifications", notes_ok,
            requires=("org.bob_to_member", "org.gina_accepts"), timeout=90.0)

    async def s_portfolio(self) -> None:
        now = datetime.now(UTC).isoformat()

        async def create() -> str:
            resp = await self.req(
                "POST", "/api/v1/portfolios", who=self.user("alice"),
                json_body={
                    "name": f"{self.prefix} portfolio",
                    "currency": "USD"
                })
            self.expect(resp, 201)
            self.portfolio_id = str(_data(resp)["id"])
            self.track("portfolio", "alice", self.portfolio_id)
            return f"portfolio {self.portfolio_id}"

        if not await self.check("portfolio.create", create,
                                requires=("auth.login_alice", )):
            return
        base = f"/api/v1/portfolios/{self.portfolio_id}"

        async def txn(body: dict[str, Any]) -> dict[str, Any]:
            resp = await self.req(
                "POST", f"{base}/transactions", who=self.user("alice"),
                json_body={
                    **body, "transaction_date": now
                })
            self.expect(resp, 201)
            result: dict[str, Any] = _data(resp)
            return result

        async def deposit() -> str:
            row = await txn({
                "ticker": "CASH",
                "transaction_type": "deposit",
                "quantity": "10000"
            })
            return f"deposit {row.get('quantity')}"

        async def buy() -> str:
            row = await txn({
                "ticker": TICKER,
                "transaction_type": "buy",
                "quantity": "10",
                "price": "100"
            })
            return f"buy {row.get('quantity')} {TICKER} @ {row.get('price')}"

        await self.check("portfolio.deposit", deposit)
        await self.check(
            "portfolio.buy", buy, requires=("portfolio.deposit", ))

        async def adjust() -> str:
            resp = await self.req(
                "GET", f"{base}/holdings", who=self.user("alice"))
            self.expect(resp, 200)
            holding = next((h for h in _data(resp) if h["ticker"] == TICKER),
                           None)
            if holding is None:
                raise CheckFailed(f"no {TICKER} holding after the buy")
            self.holding_id = str(holding["id"])
            resp = await self.req(
                "PATCH", f"{base}/holdings/{self.holding_id}",
                who=self.user("alice"), json_body={"quantity": "15"})
            self.expect(resp, 200)
            body = _data(resp)
            ids = body.get("transaction_ids") or []
            if len(ids) != 1 or Decimal(str(body["quantity"])) != 15:
                raise CheckFailed(
                    f"transaction_ids={ids} quantity={body['quantity']}")
            return (
                "PATCH holding 10 -> 15 recorded one adjustment (+5, no cash)")

        await self.check(
            "portfolio.adjustment_via_holding", adjust,
            requires=("portfolio.buy", ))

        async def sell() -> str:
            row = await txn({
                "ticker": TICKER,
                "transaction_type": "sell",
                "quantity": "4",
                "price": "120"
            })
            return f"sell {row.get('quantity')} @ {row.get('price')}"

        await self.check(
            "portfolio.sell", sell,
            requires=("portfolio.adjustment_via_holding", ))

        ledger_rows: list[dict[str, Any]] = []

        async def holdings_match() -> str:
            alice = self.user("alice")
            resp = await self.req(
                "GET", f"{base}/transactions", who=alice,
                params={"page_size": 100})
            self.expect(resp, 200)
            ledger_rows[:] = (_json(resp) or {}).get("items") or []
            implied, cash = replay_ledger(ledger_rows)
            resp = await self.req("GET", f"{base}/holdings", who=alice)
            self.expect(resp, 200)
            actual = {
                h["ticker"]: (
                    Decimal(str(h["quantity"])),
                    Decimal(str(h["average_cost"])))
                for h in _data(resp)
            }
            if set(actual) != set(implied):
                raise CheckFailed(
                    f"holdings {sorted(actual)} != ledger {sorted(implied)}")
            for ticker, (qty, cost) in implied.items():
                got_qty, got_cost = actual[ticker]
                if got_qty != qty or abs(got_cost -
                                         cost) > Decimal("0.000001"):
                    raise CheckFailed(
                        f"{ticker}: holding {got_qty}@{got_cost} "
                        f"!= ledger {qty}@{cost}")
            if implied.get(TICKER, (Decimal(0), Decimal(0)))[0] != 11:
                raise CheckFailed(
                    f"expected 11 {TICKER}, ledger says {implied}")
            return (
                f"{len(ledger_rows)} rows; {TICKER} "
                f"{fmt_decimal(implied[TICKER][0])} @ "
                f"{fmt_decimal(implied[TICKER][1])}; "
                f"implied cash {fmt_decimal(cash)}")

        await self.check(
            "portfolio.holdings_match_ledger", holdings_match,
            requires=("portfolio.sell", ))

        async def cash_match() -> str:
            _, cash = replay_ledger(ledger_rows)
            resp = await self.core_internal_get(
                f"/internal/portfolios/{self.portfolio_id}/cash-balance")
            self.expect(resp, 200)
            reported = Decimal(str(resp.json()["cash_balance"]))
            if abs(reported -
                   cash) > Decimal("0.01") or cash != Decimal("9480"):
                raise CheckFailed(
                    f"cash-balance {reported} vs ledger {cash} "
                    "(expected 9480)")
            return f"cash {reported} (internal cash-balance route) == ledger"

        await self.check(
            "portfolio.cash_matches_ledger", cash_match,
            requires=("portfolio.holdings_match_ledger", ))

        async def history_csv() -> str:
            resp = await self.req(
                "GET", f"{base}/history.csv", who=self.user("alice"))
            self.expect(resp, 200)
            if not resp.headers.get("content-type", "").startswith("text/csv"):
                raise CheckFailed(
                    f"content-type {resp.headers.get('content-type')}")
            header, rows = parse_csv(resp.text)
            wanted = [
                "date", "total_value", "cash_balance", "holdings_value",
                "daily_return"
            ]
            if header != wanted:
                raise CheckFailed(f"header {header}")
            return f"header ok, {len(rows)} snapshot row(s)"

        await self.check(
            "portfolio.history_csv", history_csv,
            requires=("portfolio.create", ))

        async def transactions_csv() -> str:
            resp = await self.req(
                "GET", f"{base}/transactions.csv", who=self.user("alice"))
            self.expect(resp, 200)
            if "attachment" not in resp.headers.get("content-disposition", ""):
                raise CheckFailed("not an attachment")
            header, rows = parse_csv(resp.text)
            if header[:3] != ["id", "transaction_date", "transaction_type"]:
                raise CheckFailed(f"header {header}")
            adjustments = [
                r for r in rows if r.get("transaction_type") == "adjustment"
            ]
            if (len(rows) != 4 or len(adjustments) != 1
                    or Decimal(adjustments[0]["quantity"]) != 5):
                raise CheckFailed(
                    f"{len(rows)} rows, adjustments={adjustments}")
            return "4 rows incl. the adjustment (+5)"

        await self.check(
            "portfolio.transactions_csv", transactions_csv,
            requires=("portfolio.sell", ))

        async def notes_ok() -> str:
            alice = self.user("alice")
            name = f"{self.prefix} portfolio"
            await self.wait_notification(
                alice, "portfolio_created", contains=name)
            await self.wait_notification(
                alice, "transaction_created", contains="adjustment +5")
            await self.wait_notification(alice, "transaction_created", count=4)
            return (
                "portfolio_created, 4x transaction_created (incl. adjustment "
                "+5)")

        await self.check(
            "portfolio.notifications", notes_ok, requires=("portfolio.sell", ))

        watchlist: dict[str, str] = {}

        async def wl_create() -> str:
            resp = await self.req(
                "POST", "/api/v1/watchlists", who=self.user("alice"),
                json_body={"name": f"{self.prefix} watchlist"})
            self.expect(resp, 201)
            watchlist["id"] = str(_data(resp)["id"])
            self.track("watchlist", "alice", watchlist["id"])
            return f"watchlist {watchlist['id']}"

        async def wl_rename() -> str:
            resp = await self.req(
                "PATCH", f"/api/v1/watchlists/{watchlist['id']}",
                who=self.user("alice"),
                json_body={"name": f"{self.prefix} renamed"})
            self.expect(resp, 200)
            return f"name={_data(resp).get('name')!r}"

        async def wl_delete() -> str:
            resp = await self.req(
                "DELETE", f"/api/v1/watchlists/{watchlist['id']}",
                who=self.user("alice"))
            self.expect(resp, 204)
            self.untrack("watchlist", watchlist["id"])
            resp = await self.req(
                "GET", "/api/v1/watchlists", who=self.user("alice"))
            if any(w["id"] == watchlist["id"] for w in _data(resp) or []):
                raise CheckFailed("still listed after delete")
            return "204, gone from the list"

        async def wl_notes() -> str:
            alice = self.user("alice")
            await self.wait_notification(alice, "watchlist_created")
            await self.wait_notification(
                alice, "watchlist_updated", contains=f"{self.prefix} renamed")
            await self.wait_notification(alice, "watchlist_deleted")
            return "created, updated, deleted"

        await self.check(
            "portfolio.watchlist_create", wl_create,
            requires=("auth.login_alice", ))
        await self.check(
            "portfolio.watchlist_rename", wl_rename,
            requires=("portfolio.watchlist_create", ))
        await self.check(
            "portfolio.watchlist_delete", wl_delete,
            requires=("portfolio.watchlist_create", ))
        await self.check(
            "portfolio.watchlist_notifications", wl_notes,
            requires=("portfolio.watchlist_delete", ))

    async def s_apikey(self) -> None:
        keys: dict[str, dict[str, str]] = {}

        async def create(name: str) -> str:
            resp = await self.req(
                "POST", "/api/v1/api-keys", who=self.user(name),
                json_body={"name": f"{self.prefix} {name} key"})
            self.expect(resp, 201)
            body = _data(resp)
            keys[name] = {"id": str(body["id"]), "key": str(body["key"])}
            self.secrets.append(keys[name]["key"])
            self.track("api_key", name, keys[name]["id"])
            return f"key {keys[name]['id']} (raw key shown once)"

        def public_path() -> tuple[str, int]:
            if self.portfolio_id is not None:
                return f"/api/v1/public/portfolios/{self.portfolio_id}", 200
            return f"/api/v1/public/portfolios/{uuid4()}", 404

        async def alice_get() -> str:
            path, code = public_path()
            resp = await self.req("GET", path, api_key=keys["alice"]["key"])
            self.expect(resp, code)
            return f"GET public portfolio with X-API-Key -> {code}"

        async def revoke(name: str) -> str:
            resp = await self.req(
                "DELETE", f"/api/v1/api-keys/{keys[name]['id']}",
                who=self.user(name))
            self.expect(resp, 204)
            self.untrack("api_key", keys[name]["id"])
            return "204"

        async def revoked_401() -> str:
            path, _ = public_path()
            resp = await self.req("GET", path, api_key=keys["alice"]["key"])
            self.expect(resp, 401)
            code = ((_json(resp) or {}).get("error") or {}).get("code")
            return f"401 {code}"

        async def put_holding(quantity: str, delta: str) -> dict[str, Any]:
            path = (
                f"/api/v1/public/portfolios/{self.portfolio_id}"
                f"/holdings/{self.holding_id}")
            resp = await self.req(
                "PUT", path, api_key=keys["alice"]["key"],
                json_body={"quantity": quantity})
            self.expect(resp, 200)
            body: dict[str, Any] = _data(resp)
            ids = [str(i) for i in body.get("transaction_ids") or []]
            if (Decimal(str(body["quantity"])) != Decimal(quantity)
                    or len(ids) != 1):
                raise CheckFailed(
                    f"quantity={body['quantity']} transaction_ids={ids}")
            resp = await self.req(
                "GET", f"/api/v1/portfolios/{self.portfolio_id}/transactions",
                who=self.user("alice"), params={"page_size": 100})
            self.expect(resp, 200)
            row = next((
                r for r in (_json(resp) or {}).get("items") or []
                if str(r.get("id")) == ids[0]), None)
            if (row is None or row.get("transaction_type") != "adjustment"
                    or Decimal(str(row.get("quantity"))) != Decimal(delta)):
                raise CheckFailed(f"ledger row for {ids[0]}: {row}")
            return body

        async def alice_put_holding() -> str:
            await put_holding("12", "1")
            await put_holding("11", "-1")
            resp = await self.core_internal_get(
                f"/internal/portfolios/{self.portfolio_id}/cash-balance")
            self.expect(resp, 200)
            cash = Decimal(str(resp.json()["cash_balance"]))
            if cash != Decimal("9480"):
                raise CheckFailed(
                    f"cash {cash} after adjustments (expected 9480)")
            alice = self.user("alice")
            await self.wait_notification(
                alice, "transaction_created", contains="(API key)", count=2)
            return (
                "PUT JSON body 11 -> 12 -> 11: two adjustments (+1, -1), "
                "cash unchanged, 2 notifications")

        await self.check(
            "apikey.create", lambda: create("alice"),
            requires=("auth.login_alice", ))
        await self.check(
            "apikey.public_get", alice_get, requires=("apikey.create", ))
        await self.check(
            "apikey.public_put_holding", alice_put_holding,
            requires=("apikey.create", "portfolio.holdings_match_ledger"))
        await self.check(
            "apikey.revoke", lambda: revoke("alice"),
            requires=("apikey.create", ))
        await self.check(
            "apikey.revoked_key_401", revoked_401,
            requires=("apikey.revoke", ))

        async def notes_ok() -> str:
            alice = self.user("alice")
            await self.wait_notification(alice, "api_key_created")
            await self.wait_notification(alice, "api_key_revoked")
            return "api_key_created, api_key_revoked"

        await self.check(
            "apikey.notifications", notes_ok, requires=("apikey.revoke", ))

        probe = f"/api/v1/public/portfolios/{uuid4()}"

        async def bob_works() -> str:
            resp = await self.req("GET", probe, api_key=keys["bob"]["key"])
            self.expect(resp, 404)
            return "accepted (404 for an unknown portfolio)"

        async def bob_deactivated() -> str:
            resp = await self.req("GET", probe, api_key=keys["bob"]["key"])
            self.expect(resp, 401)
            code = ((_json(resp) or {}).get("error") or {}).get("code")
            if code != "account_deactivated":
                raise CheckFailed(f"error code {code!r}")
            return "401 account_deactivated"

        await self.check(
            "apikey.bob_key_create", lambda: create("bob"),
            requires=("auth.login_bob", ))
        await self.check(
            "apikey.bob_key_works", bob_works,
            requires=("apikey.bob_key_create", ))
        deactivated = await self.check(
            "apikey.deactivate_bob", self._deactivate_bob,
            requires=("apikey.bob_key_works", "auth.admin_token"))
        try:
            await self.check(
                "apikey.deactivated_owner_key_401", bob_deactivated,
                requires=("apikey.deactivate_bob", ))
        finally:
            if deactivated:
                await self.check("apikey.reactivate_bob", self._reactivate_bob)
        await self.check(
            "apikey.reactivated_owner_key_works", bob_works,
            requires=("apikey.reactivate_bob", ))
        await self.check(
            "apikey.bob_key_revoke", lambda: revoke("bob"),
            requires=("apikey.bob_key_create", ))

    async def rag_citations(self, who: Actor,
                            question: str) -> tuple[list[dict[str, Any]], str]:
        body = {"question": question, "ticker": TICKER}
        for _ in range(3):
            resp = await self.req(
                "POST", "/api/v1/rag/ask", who=who, json_body=body,
                timeout=180.0)
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("retry-after", "10")), 65)
                self.info(f"rag rate limited, waiting {wait}s")
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 200:
                return list(_data(resp).get("citations") or []), "ask"
            if resp.status_code != 503:
                self.expect(resp, 200)
            break
        token = await self.bearer(who)
        async with self.api.stream("POST", "/api/v1/rag/ask/stream", json=body,
                                   timeout=180.0,
                                   headers={"Authorization": f"Bearer {token}"
                                            }) as stream:
            if stream.status_code != 200:
                await stream.aread()
                self.expect(stream, 200)
            event = ""
            async for line in stream.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "sources":
                    sources = json.loads(line[5:].strip())
                    return (
                        list(sources.get("citations") or []), "stream-sources")
        raise CheckFailed("no sources event in /rag/ask/stream")

    async def s_documents(self) -> None:
        marker = f"zq{self.run_id}quasar"
        lines = [
            f"{self.prefix} research memo about {TICKER}.",
            f"The unique marker phrase {marker} identifies this e2e document.",
            f"{marker}: the fictional Quasar accelerator line of {TICKER} "
            "shipped ahead of plan, according to this synthetic test memo.",
            "This text exists only to test ingestion, retrieval and deletion."
        ]
        text_body = ("\n\n".join(lines * 3) + "\n").encode("utf-8")
        pdf_body = build_pdf(lines)
        question = f"What does the memo say about {marker}?"
        alice_name = "alice"

        for kind, body, mime in (("txt", text_body, "text/plain"),
                                 ("pdf", pdf_body, "application/pdf")):

            async def upload(
                    kind: str = kind, body: bytes = body,
                    mime: str = mime) -> str:
                resp = await self.req(
                    "POST", "/api/v1/research/documents",
                    who=self.user(alice_name), files={
                        "file": (f"e2e-{self.run_id}-memo.{kind}", body, mime)
                    }, data={
                        "category": "analyst_report",
                        "ticker": TICKER,
                        "title": f"{self.prefix} memo {kind}"
                    }, timeout=60.0)
                self.expect(resp, 201)
                doc = _data(resp)
                self.documents[kind] = doc
                self.track("document", alice_name, str(doc["id"]))
                return (
                    f"{doc['id']} status={doc['status']} ({len(body)} bytes)")

            await self.check(
                f"documents.upload_{kind}", upload,
                requires=("auth.login_alice", ))

        async def ready() -> str:
            started = time.monotonic()
            pending = dict(self.documents)
            done: dict[str, float] = {}
            while pending:
                for kind, doc in list(pending.items()):
                    resp = await self.req(
                        "GET", f"/api/v1/research/documents/{doc['id']}",
                        who=self.user(alice_name))
                    self.expect(resp, 200)
                    status = _data(resp)["status"]
                    if status == "failed":
                        raise CheckFailed(
                            f"{kind} ingestion failed (status=failed)")
                    if status == "ready":
                        done[kind] = time.monotonic() - started
                        del pending[kind]
                if pending:
                    if time.monotonic() - started > DOCUMENT_READY_TIMEOUT:
                        raise CheckFailed(
                            f"not ready after {DOCUMENT_READY_TIMEOUT:g}s: "
                            f"{sorted(pending)} (ingestion needs ai-service + "
                            "embeddings)")
                    await asyncio.sleep(3.0)
            return ", ".join(
                f"{k} ready after {v:.0f}s" for k, v in sorted(done.items()))

        await self.check(
            "documents.ready", ready,
            requires=("documents.upload_txt", "documents.upload_pdf"),
            timeout=DOCUMENT_READY_TIMEOUT + 30)

        async def ready_notes() -> str:
            alice = self.user(alice_name)
            await self.wait_notification(alice, "document_uploaded", count=2)
            await self.wait_notification(
                alice, "document_ready", contains=f"{self.prefix} memo",
                count=2)
            return "2x document_uploaded, 2x document_ready"

        await self.check(
            "documents.notifications", ready_notes,
            requires=("documents.ready", ))

        async def preview() -> str:
            out = []
            for kind, expected_type in (("txt", "text/plain"),
                                        ("pdf", "application/pdf")):
                doc = self.documents[kind]
                resp = await self.req(
                    "GET", f"/api/v1/research/documents/{doc['id']}/preview",
                    who=self.user(alice_name))
                self.expect(resp, 200)
                disposition = resp.headers.get("content-disposition", "")
                ctype = resp.headers.get("content-type", "")
                if not disposition.startswith(
                        "inline") or not ctype.startswith(expected_type):
                    raise CheckFailed(f"{kind}: {ctype!r} {disposition!r}")
                wanted = text_body if kind == "txt" else pdf_body
                if resp.content != wanted:
                    raise CheckFailed(
                        f"{kind}: preview bytes differ from the upload")
                out.append(f"{kind} inline {ctype.split(';')[0]}")
            return ", ".join(out)

        await self.check(
            "documents.preview", preview,
            requires=("documents.upload_txt", "documents.upload_pdf"))

        async def object_exists() -> str:
            for kind, doc in self.documents.items():
                if not await asyncio.to_thread(self.storage_head,
                                               doc["storage_url"]):
                    raise CheckFailed(
                        f"{kind}: object {doc['storage_url']} missing")
            return f"{len(self.documents)} object(s) in the documents bucket"

        await self.check(
            "documents.object_exists", object_exists,
            requires=("documents.upload_txt", "documents.upload_pdf"))

        def own_ids() -> set[str]:
            return {str(d["id"]) for d in self.documents.values()}

        async def ask_own() -> str:
            citations, how = await self.rag_citations(
                self.user(alice_name), question)
            hits = [
                c for c in citations if str(c.get("document_id")) in own_ids()
            ]
            if not hits:
                ids = [c.get("document_id") for c in citations]
                raise CheckFailed(
                    f"{how}: alice's document not cited; cited {ids}")
            if how != "ask":
                raise CheckFailed(
                    "POST /rag/ask did not answer (503, LLM unavailable?); "
                    f"retrieval via the stream cites alice's doc "
                    f"[{hits[0]['index']}]")
            return f"cited as [{hits[0]['index']}] of {len(citations)}"

        await self.check(
            "documents.rag_ask_cites_own", ask_own,
            requires=("documents.ready", ), timeout=240.0)

        async def ask_bob() -> str:
            citations, how = await self.rag_citations(
                self.user("bob"), question)
            leaked = [
                c for c in citations if str(c.get("document_id")) in own_ids()
            ]
            if leaked:
                raise CheckFailed(
                    "bob's answer cites alice's private document")
            return (
                f"{how}: {len(citations)} citation(s), none of alice's "
                f"private docs")

        await self.check(
            "documents.rag_scope_other_user", ask_bob,
            requires=("documents.ready", "auth.login_bob"), timeout=240.0)

        async def list_system() -> str:
            alice = self.user(alice_name)
            resp = await self.req(
                "GET", "/api/v1/research/documents", who=alice,
                params={"page_size": 100})
            self.expect(resp, 200)
            items = (_json(resp) or {}).get("items") or []
            if any(i.get("is_system") for i in items):
                raise CheckFailed("default list contains system documents")
            if not own_ids() <= {str(i["id"]) for i in items}:
                raise CheckFailed("default list misses alice's documents")
            resp = await self.req(
                "GET", "/api/v1/research/documents", who=alice, params={
                    "page_size": 100,
                    "include_system": "true"
                })
            self.expect(resp, 200)
            body = _json(resp) or {}
            system = [i for i in body.get("items") or [] if i.get("is_system")]
            if not system:
                raise SkipCheck(
                    "no system documents in this deployment "
                    "(upload_seed_documents --as-system not run?)")
            return (
                f"{len(system)} system doc(s) on page 1, "
                f"total={body.get('total')}")

        await self.check(
            "documents.list_include_system", list_system,
            requires=("documents.upload_txt", ))

        async def delete() -> str:
            for doc in self.documents.values():
                resp = await self.req(
                    "DELETE", f"/api/v1/research/documents/{doc['id']}",
                    who=self.user(alice_name))
                self.expect(resp, 204)
                self.untrack("document", str(doc["id"]))
                resp = await self.req(
                    "GET", f"/api/v1/research/documents/{doc['id']}",
                    who=self.user(alice_name))
                self.expect(resp, 404)
            return f"{len(self.documents)} deleted (204, then 404)"

        await self.check(
            "documents.delete", delete, requires=("documents.upload_txt", ))

        async def object_gone() -> str:
            for kind, doc in self.documents.items():
                if await asyncio.to_thread(self.storage_head,
                                           doc["storage_url"]):
                    raise CheckFailed(f"{kind}: object still in storage")
            return "objects removed from storage"

        await self.check(
            "documents.object_gone", object_gone,
            requires=("documents.delete", ))

        async def ask_after_delete() -> str:
            started = time.monotonic()
            while True:
                citations, how = await self.rag_citations(
                    self.user(alice_name), question)
                still = [
                    c for c in citations
                    if str(c.get("document_id")) in own_ids()
                ]
                if not still:
                    return (
                        f"{how}: no longer cited after "
                        f"{time.monotonic() - started:.0f}s")
                if time.monotonic() - started > 45:
                    raise CheckFailed("deleted document still cited after 45s")
                await asyncio.sleep(8.0)

        await self.check(
            "documents.rag_after_delete", ask_after_delete,
            requires=("documents.delete", "documents.ready"), timeout=300.0)

    async def s_research(self) -> None:

        async def create_note() -> str:
            resp = await self.req(
                "POST", "/api/v1/research/notes", who=self.user("alice"),
                json_body={
                    "title": f"{self.prefix} {TICKER} outlook",
                    "content": (
                        f"{TICKER} delivered an excellent quarter: "
                        "strong data-center growth, record margins "
                        "and a very bullish outlook."),
                    "organization_id": self.org_id,
                    "tags": [TICKER]
                })
            self.expect(resp, 201)
            self.research_note_id = str(_data(resp)["id"])
            self.track("note", "alice", self.research_note_id)
            return f"note {self.research_note_id} (organization note)"

        await self.check(
            "research.note_create", create_note,
            requires=("auth.login_alice", "org.create"))

        comments_path = (
            f"/api/v1/research/notes/{self.research_note_id}/comments")

        async def create_comment() -> str:
            resp = await self.req(
                "POST", comments_path, who=self.user("alice"), json_body={
                    "content": (
                        f"Great execution by {TICKER}, I am very "
                        "optimistic about the next quarters.")
                })
            self.expect(resp, 201)
            self.research_comment_id = str(_data(resp)["id"])
            return f"comment {self.research_comment_id}"

        await self.check(
            "research.comment_create", create_comment,
            requires=("research.note_create", ))

        async def note_sentiment() -> str:

            async def attempt() -> dict[str, Any] | None:
                resp = await self.req(
                    "GET", f"/api/v1/research/notes/{self.research_note_id}",
                    who=self.user("alice"))
                self.expect(resp, 200)
                note = _data(resp)
                return note if note.get("sentiment_label") else None

            note = await poll(attempt, 60.0, 3.0)
            if note is None:
                raise CheckFailed(
                    "sentiment still null after 60s (ai-service "
                    "/internal/sentiment needs the LLM)")
            return f"{note['sentiment_label']} score={note['sentiment_score']}"

        async def comment_sentiment() -> str:

            async def attempt() -> dict[str, Any] | None:
                resp = await self.req(
                    "GET", comments_path, who=self.user("alice"))
                self.expect(resp, 200)
                for c in (_json(resp) or {}).get("items") or []:
                    if c["id"] == self.research_comment_id and c.get(
                            "sentiment_label"):
                        return dict(c)
                return None

            comment = await poll(attempt, 60.0, 3.0)
            if comment is None:
                raise CheckFailed("comment sentiment still null after 60s")
            return (
                f"{comment['sentiment_label']} "
                f"score={comment['sentiment_score']}")

        await self.check(
            "research.note_sentiment", note_sentiment,
            requires=("research.note_create", ), timeout=90.0)
        await self.check(
            "research.comment_sentiment", comment_sentiment,
            requires=("research.comment_create", ), timeout=90.0)

        async def summary() -> str:
            resp = await self.req(
                "GET", "/api/v1/research/sentiment/summary",
                who=self.user("alice"), params={"ticker": TICKER})
            self.expect(resp, 200)
            body = _json(resp) or {}
            if body.get("notes", 0) < 1 or body.get("comments", 0) < 1:
                raise CheckFailed(
                    f"notes={body.get('notes')} "
                    f"comments={body.get('comments')}")
            return (
                f"total={body.get('total')} notes={body.get('notes')} "
                f"comments={body.get('comments')} "
                f"avg={body.get('average_score')}")

        await self.check(
            "research.sentiment_summary", summary,
            requires=("research.note_sentiment", "research.comment_sentiment"))

        async def comment_ids(who: Actor) -> dict[str, dict[str, Any]]:
            resp = await self.req(
                "GET", comments_path, who=who, params={"page_size": 100})
            self.expect(resp, 200)
            return {c["id"]: c for c in (_json(resp) or {}).get("items") or []}

        async def hide() -> str:
            resp = await self.req(
                "POST",
                f"/api/v1/moderation/comments/{self.research_comment_id}/hide",
                who=ADMIN)
            self.expect(resp, 200)
            if _data(resp).get("is_hidden") is not True:
                raise CheckFailed("is_hidden is not true")
            return "admin (content:moderate) hid alice's comment"

        await self.check(
            "research.hide_comment", hide,
            requires=("research.comment_create", "auth.admin_token"))

        async def hidden_for_bob() -> str:
            if self.research_comment_id in await comment_ids(self.user("bob")):
                raise CheckFailed("bob still sees the hidden comment")
            return "bob (org member) no longer sees it"

        async def hidden_for_author() -> str:
            comment = (await comment_ids(self.user("alice")
                                         )).get(str(self.research_comment_id))
            if comment is None or comment.get("is_hidden") is not True:
                raise CheckFailed(f"alice sees {comment!r}")
            return "alice sees it with is_hidden=true"

        async def hidden_note() -> str:
            await self.wait_notification(self.user("alice"), "comment_hidden")
            return "alice got comment_hidden"

        async def unhide() -> str:
            resp = await self.req(
                "POST",
                f"/api/v1/moderation/comments/{self.research_comment_id}"
                f"/unhide", who=ADMIN)
            self.expect(resp, 200)
            if self.research_comment_id not in await comment_ids(
                    self.user("bob")):
                raise CheckFailed("bob does not see the comment after unhide")
            return "unhidden, bob sees it again"

        dep = ("research.hide_comment", )
        await self.check(
            "research.hidden_for_other_member", hidden_for_bob,
            requires=(*dep, "auth.login_bob"))
        await self.check(
            "research.hidden_marked_for_author", hidden_for_author,
            requires=dep)
        await self.check(
            "research.comment_hidden_notification", hidden_note, requires=dep)
        await self.check("research.unhide_comment", unhide, requires=dep)

    async def s_realtime(self) -> None:
        sockets: list[Any] = []
        name = f"{self.prefix} realtime"

        async def open_sockets() -> str:
            alice = self.user("alice")
            token = _need(await self.bearer(alice), "token")
            for _ in range(2):
                sockets.append(
                    await self.ws_open(f"/ws/user:{alice.user_id}", token))
            await asyncio.sleep(1.0)
            return "2 sockets on user:<alice>"

        async def push() -> str:
            alice = self.user("alice")
            started = time.monotonic()
            resp = await self.req(
                "POST", "/api/v1/watchlists", who=alice,
                json_body={"name": name})
            self.expect(resp, 201)
            self.track("watchlist", "alice", str(_data(resp)["id"]))
            per_socket = await asyncio.gather(
                *(self.ws_json_messages(ws, 12.0) for ws in sockets))
            counts = []
            for messages in per_socket:
                matching = [
                    m for m in messages if m.get("event") == "notification" and
                    (m.get("payload") or {}).get("type") == "watchlist_created"
                    and name in str((m.get("payload") or {}).get("content"))
                ]
                counts.append(len(matching))
            if counts != [1, 1]:
                raise CheckFailed(
                    f"copies per socket {counts} (expected [1, 1])")
            return (
                f"each socket got exactly one watchlist_created push "
                f"(window {time.monotonic() - started:.0f}s)")

        try:
            await self.check(
                "realtime.sockets_open", open_sockets,
                requires=("auth.login_alice", ))
            await self.check(
                "realtime.push_exactly_once_per_socket", push,
                requires=("realtime.sockets_open", ))
        finally:
            for ws in sockets:
                with contextlib.suppress(Exception):
                    await ws.close()

    async def s_yjs(self) -> None:
        from websockets.exceptions import ConnectionClosed

        note_id = self.org_note_id
        path = f"/ws/yjs/{note_id}"
        rest = f"/api/v1/research/notes/{note_id}"
        peers: dict[str, tuple[Any, YClient]] = {}
        opened_at = 0.0

        async def join(label: str, who: Actor) -> str:
            token = _need(await self.bearer(who), "token")
            ws = await self.ws_open(path, token)
            client = YClient()
            first = await asyncio.wait_for(ws.recv(), timeout=10.0)
            if not isinstance(first, bytes):
                raise CheckFailed(
                    f"first frame is {type(first).__name__}, not binary")
            client.apply(first)
            peers[label] = (ws, client)
            return (
                f"{who.name}: initial state {len(first)} bytes, "
                f"text={client.value()!r}")

        async def receive_into(label: str, timeout: float) -> int:
            ws, client = peers[label]
            count = 0
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    frame = await asyncio.wait_for(
                        ws.recv(), timeout=remaining)
                except (TimeoutError, ConnectionClosed):
                    break
                if isinstance(frame, bytes):
                    client.apply(frame)
                    count += 1
            return count

        async def send_and_sync(sender: str, receiver: str, chunk: str) -> str:
            ws, client = peers[sender]
            await ws.send(client.append(chunk))
            other = peers[receiver][1]
            deadline = time.monotonic() + 10.0
            while other.value() != client.value():
                if time.monotonic() > deadline:
                    raise CheckFailed(
                        f"{receiver} has {other.value()!r}, "
                        f"{sender} has {client.value()!r}")
                await receive_into(receiver, 1.0)
            return f"{receiver} converged ({len(client.value())} chars)"

        try:

            async def join_alice() -> str:
                nonlocal opened_at
                opened_at = time.monotonic()
                return await join("alice", self.user("alice"))

            base = ("org.note_create", "org.member_can_comment")
            await self.check("yjs.connect_alice", join_alice, requires=base)
            await self.check(
                "yjs.connect_bob_member",
                lambda: join("bob", self.user("bob")),
                requires=("yjs.connect_alice", ))
            both = ("yjs.connect_alice", "yjs.connect_bob_member")
            await self.check(
                "yjs.alice_to_bob",
                lambda: send_and_sync("alice", "bob", " [alice-1]"),
                requires=both)
            await self.check(
                "yjs.bob_to_alice",
                lambda: send_and_sync("bob", "alice", " [bob-1]"),
                requires=both)

            async def rest_409() -> str:
                resp = await self.req(
                    "PATCH", rest, who=self.user("alice"),
                    json_body={"content": "REST body edit"})
                self.expect(resp, 409)
                return "PATCH content while the room is open -> 409"

            async def rest_title() -> str:
                resp = await self.req(
                    "PATCH", rest, who=self.user("alice"),
                    json_body={"title": f"{self.prefix} org note (live)"})
                self.expect(resp, 200)
                return "PATCH title only -> 200"

            await self.check(
                "yjs.rest_body_edit_409", rest_409,
                requires=("yjs.connect_alice", ))
            await self.check(
                "yjs.rest_title_edit_allowed", rest_title,
                requires=("yjs.connect_alice", ))

            async def garbage() -> str:
                await join("mallory", self.user("alice"))
                ws, _ = peers["mallory"]
                await ws.send(
                    b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\x0fgarbage")
                code = await self.ws_close_code(ws, 10.0)
                peers.pop("mallory", None)
                if code != 1008:
                    raise CheckFailed(f"close code {code} (expected 1008)")
                return "third client closed with 1008"

            await self.check(
                "yjs.garbage_update_closed_1008", garbage, requires=both)

            await self.check(
                "yjs.still_syncing_after_garbage",
                lambda: send_and_sync("alice", "bob", " [alice-2]"),
                requires=both)

            async def viewer_dropped() -> str:
                await join("gina", self.user("gina"))
                ws, client = peers["gina"]
                await ws.send(client.append(" [VIEWER-INJECT]"))
                got = await receive_into("alice", 3.0)
                if "VIEWER-INJECT" in peers["alice"][1].value():
                    raise CheckFailed("the viewer's update reached alice")
                return (
                    "viewer joined and synced; its update was dropped "
                    f"({got} frames)")

            await self.check(
                "yjs.viewer_update_dropped", viewer_dropped,
                requires=(*both, "org.gina_accepts"))

            async def persisted() -> str:
                elapsed = time.monotonic() - opened_at
                wait = YJS_SNAPSHOT_DEBOUNCE_SECONDS + 1 - elapsed
                if wait > 0:
                    self.info(
                        f"yjs: waiting {wait:.0f}s for the snapshot debounce")
                    await asyncio.sleep(wait)
                await send_and_sync("alice", "bob", " [final]")
                expected = peers["alice"][1].value()

                async def attempt() -> str | None:
                    resp = await self.req("GET", rest, who=self.user("alice"))
                    self.expect(resp, 200)
                    content = _data(resp).get("content")
                    return str(content) if content == expected else None

                if await poll(attempt, 20.0, 1.0) is None:
                    raise CheckFailed(
                        "REST content != collaborative text 20s after "
                        "the debounce")
                if "VIEWER-INJECT" in expected:
                    raise CheckFailed(
                        "persisted text contains the viewer's update")
                return (
                    f"REST content == Y.Text 'content' ({len(expected)} chars)"
                )

            await self.check(
                "yjs.persisted_to_rest", persisted, requires=both,
                timeout=90.0)
        finally:
            for ws, _ in list(peers.values()):
                with contextlib.suppress(Exception):
                    await ws.close()
            peers.clear()

        async def after_close() -> str:

            async def attempt() -> httpx.Response | None:
                resp = await self.req(
                    "PATCH", rest, who=self.user("alice"),
                    json_body={"content": f"{self.prefix} closed"})
                return resp if resp.status_code == 200 else None

            if await poll(attempt, 15.0, 1.0) is None:
                raise CheckFailed(
                    "REST body edit still refused 15s after all "
                    "sockets closed")
            return "room closed: REST body edit -> 200"

        await self.check(
            "yjs.rest_body_edit_after_close", after_close,
            requires=("yjs.persisted_to_rest", ))

    async def s_analytics(self) -> None:
        overview_keys = {
            "range", "scope", "total_analyses", "status_counts",
            "recommendations", "action_distribution", "average_confidence",
            "accuracy", "by_agent", "by_ticker"
        }

        async def overview(who: Who, scope: str) -> str:
            resp = await self.req(
                "GET", "/api/v1/analytics/overview", who=who,
                params={"scope": scope})
            self.expect(resp, 200)
            body = _data(resp)
            missing = overview_keys - set(body)
            if missing or not {"from", "to"} <= set(body["range"]):
                raise CheckFailed(
                    f"missing keys {sorted(missing)} / range "
                    f"{body.get('range')}")
            if body["scope"] != scope:
                raise CheckFailed(f"scope {body['scope']!r}")
            return f"scope={scope} total_analyses={body['total_analyses']}"

        await self.check(
            "analytics.overview_mine",
            lambda: overview(self.user("alice"), "mine"),
            requires=("auth.login_alice", ))
        await self.check(
            "analytics.overview_all_admin", lambda: overview(ADMIN, "all"),
            requires=("auth.admin_token", ))

        async def all_forbidden() -> str:
            resp = await self.req(
                "GET", "/api/v1/analytics/overview", who=self.user("alice"),
                params={"scope": "all"})
            self.expect(resp, 403)
            return "scope=all for a user -> 403"

        await self.check(
            "analytics.overview_all_forbidden", all_forbidden,
            requires=("auth.login_alice", ))

        async def timeseries() -> str:
            resp = await self.req(
                "GET", "/api/v1/analytics/timeseries", who=self.user("alice"),
                params={
                    "metric": "analyses",
                    "interval": "week"
                })
            self.expect(resp, 200)
            body = _data(resp)
            points = body.get("points")
            if body.get("metric") != "analyses" or not isinstance(points,
                                                                  list):
                raise CheckFailed(f"body {body}")
            if points and not {"period_start", "value", "count"} <= set(
                    points[0]):
                raise CheckFailed(f"point {points[0]}")
            return f"{len(points)} weekly point(s)"

        await self.check(
            "analytics.timeseries", timeseries,
            requires=("auth.login_alice", ))

        async def export_csv() -> str:
            resp = await self.req(
                "GET", "/api/v1/analytics/export.csv", who=self.user("alice"),
                params={"dataset": "analyses"})
            self.expect(resp, 200)
            if not resp.headers.get("content-type", "").startswith("text/csv"):
                raise CheckFailed(
                    f"content-type {resp.headers.get('content-type')}")
            header, rows = parse_csv(resp.text)
            if not header:
                raise CheckFailed("empty CSV (no header row)")
            truncated = resp.headers.get("x-export-truncated")
            return (
                f"{len(header)} columns, {len(rows)} row(s), "
                f"truncated={truncated}")

        await self.check(
            "analytics.export_csv", export_csv,
            requires=("auth.login_alice", ))

    async def s_backtest(self) -> None:
        from shared.market_calendar import previous_trading_day

        today = datetime.now(UTC).date()
        day2 = previous_trading_day(previous_trading_day(today))
        day1 = previous_trading_day(day2)

        async def needs_two() -> str:
            resp = await self.req(
                "POST", "/api/v1/backtests/portfolio", who=self.user("alice"),
                json_body={"portfolio_id": self.portfolio_id})
            self.expect(resp, 422)
            message = str(((_json(resp) or {}).get("error")
                           or {}).get("message"))
            if "daily snapshot(s)" not in message:
                raise CheckFailed(
                    f"422 without the snapshot reason: {message!r}")
            return "422 before any snapshot: " + one_line(message, 90)

        await self.check(
            "backtest.needs_two_snapshots", needs_two,
            requires=("portfolio.create", ))

        async def seed() -> str:
            from database import SessionLocal
            from domains.portfolio.service import PortfolioService

            self._db_used = True
            pid = UUID(_need(self.portfolio_id, "portfolio"))
            async with SessionLocal() as session:
                service = PortfolioService(session)
                holdings = await service.holdings.list_for_portfolio(pid)
                values = []
                for day in (day1, day2):
                    snap = await service._compute_snapshot(
                        pid, day, holdings, {})
                    values.append(f"{day}={fmt_decimal(snap.total_value)}")
                await session.commit()
            return "e2e portfolio only, holdings at cost: " + ", ".join(values)

        await self.check(
            "backtest.seed_snapshots", seed, requires=("portfolio.create", ))

        async def create() -> str:
            resp = await self.req(
                "POST", "/api/v1/backtests/portfolio", who=self.user("alice"),
                json_body={
                    "portfolio_id": self.portfolio_id,
                    "start_date": day1.isoformat(),
                    "end_date": day2.isoformat()
                })
            self.expect(resp, 201)
            self.backtest_id = str(_data(resp)["id"])
            self.track("backtest", "alice", self.backtest_id)
            return (
                f"backtest {self.backtest_id} status={_data(resp)['status']}")

        await self.check(
            "backtest.create", create, requires=("backtest.seed_snapshots", ))

        async def completed() -> str:
            started = time.monotonic()

            async def attempt() -> dict[str, Any] | None:
                resp = await self.req(
                    "GET", f"/api/v1/backtests/{self.backtest_id}",
                    who=self.user("alice"))
                self.expect(resp, 200)
                body: dict[str, Any] = _data(resp)
                return (
                    body if body["status"] in ("completed",
                                               "failed") else None)

            body = await poll(attempt, 180.0, 3.0)
            if body is None:
                raise CheckFailed("not finished after 180s")
            if body["status"] != "completed":
                notes = await self.notifications(self.user("alice"))
                why = next((
                    n.get("content")
                    for n in notes if n.get("type") == "backtest_failed"), "")
                raise CheckFailed(f"status=failed {why}")
            return (
                f"completed after {time.monotonic() - started:.0f}s, "
                f"strategy_return={body.get('strategy_return')}")

        await self.check(
            "backtest.completed", completed, requires=("backtest.create", ),
            timeout=200.0)

        async def note() -> str:
            await self.wait_notification(
                self.user("alice"), "backtest_completed")
            return "backtest_completed notification"

        await self.check(
            "backtest.notification", note, requires=("backtest.completed", ))

    async def s_analysis(self) -> None:
        if not self.args.with_analysis:
            self.record(
                "analysis.all", SKIP, "pass --with-analysis (runs the LLM "
                "agents, up to 15 min)")
            return
        state: dict[str, Any] = {
            "events": [],
            "analysis_id": None,
            "trace": [],
            "connects": 0
        }
        run_started = time.monotonic()

        async def create() -> str:
            resp = await self.req(
                "POST", "/api/v1/analyses", who=self.user("alice"), json_body={
                    "ticker": TICKER,
                    "portfolio_id": self.portfolio_id
                })
            self.expect(resp, 201)
            body = _data(resp)
            state["analysis_id"] = str(body["id"])
            self.analysis_ids.append(str(body["id"]))
            return f"analysis {body['id']} status={body['status']}"

        if not await self.check("analysis.create", create,
                                requires=("portfolio.create", )):
            return
        analysis_id = str(state["analysis_id"])
        events: list[str] = state["events"]
        stop = asyncio.Event()

        async def listen() -> None:
            from websockets.exceptions import ConnectionClosed

            while not stop.is_set():
                try:
                    token = _need(
                        await self.bearer(self.user("alice")), "token")
                    ws = await self.ws_open(
                        f"/ws/analysis:{analysis_id}", token)
                    state["connects"] += 1
                    state["trace"].append(
                        f"+{time.monotonic() - run_started:.0f}s connect")
                    if "connected_status" not in state:
                        state["connected_status"] = (
                            await self._analysis_status(analysis_id))
                    try:
                        while not stop.is_set():
                            try:
                                raw = await asyncio.wait_for(
                                    ws.recv(), timeout=2.0)
                            except TimeoutError:
                                continue
                            with contextlib.suppress(ValueError):
                                name = str(json.loads(raw).get("event"))
                                events.append(name)
                                state["trace"].append(
                                    f"+{time.monotonic() - run_started:.0f}s "
                                    f"{name}")
                    finally:
                        await ws.close()
                except ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd is not None else None
                    state["trace"].append(
                        f"+{time.monotonic() - run_started:.0f}s closed {code}"
                    )
                    await asyncio.sleep(1.0)
                except Exception as exc:
                    state["listen_error"] = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(5.0)

        listener = asyncio.create_task(listen())
        final: dict[str, Any] = {}

        async def finished() -> str:
            started = time.monotonic()
            last_print = started
            while True:
                status = await self._analysis_status(analysis_id)
                if status in ("completed", "failed"):
                    final["status"] = status
                    state["status_seen"] = round(
                        time.monotonic() - run_started)
                    break
                if time.monotonic() - last_print >= 30:
                    last_print = time.monotonic()
                    self.info(
                        f"analysis {analysis_id}: status={status} "
                        f"after {last_print - started:.0f}s")
                if time.monotonic() - started > ANALYSIS_TIMEOUT:
                    raise CheckFailed(
                        f"still {status} after {ANALYSIS_TIMEOUT:g}s")
                await asyncio.sleep(10.0)
            if final["status"] != "completed":
                resp = await self.req(
                    "GET", f"/api/v1/analyses/{analysis_id}",
                    who=self.user("alice"))
                reason = (_data(resp) or {}).get("failure_reason")
                raise CheckFailed(f"analysis failed: {reason}")
            return f"completed after {time.monotonic() - started:.0f}s"

        try:
            await self.check(
                "analysis.completed", finished, timeout=ANALYSIS_TIMEOUT + 60)
            wanted_event = f"analysis.{final.get('status')}"
            deadline = time.monotonic() + 30.0
            while wanted_event not in events and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
            if wanted_event in events:
                await asyncio.sleep(1.0)
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(listener, timeout=10.0)

        async def ws_snapshot() -> str:
            if not events:
                raise CheckFailed(
                    f"no message on analysis:<id> "
                    f"{state.get('listen_error', '')}")
            if events[0] != "analysis.status":
                raise CheckFailed(
                    f"first message {events[0]!r}, expected "
                    f"analysis.status; events seen: {events}")
            started = (
                "; analysis.started also received"
                if "analysis.started" in events else "")
            return (
                f"first message analysis.status (status "
                f"{state.get('connected_status')!r} at connect){started}")

        async def ws_final() -> str:
            status = _need(final.get("status"), "a finished analysis")
            wanted = f"analysis.{status}"
            if wanted not in events:
                raise CheckFailed(
                    f"no {wanted} event; trace: {'; '.join(state['trace'])} "
                    f"{state.get('listen_error', '')} (core-service log: "
                    f"websocket_analysis_broadcast analysis_id={analysis_id})")
            return (
                f"events: {', '.join(dict.fromkeys(events))}; "
                f"status seen +{state.get('status_seen', '?')}s, trace: "
                f"{'; '.join(state['trace'][-3:])}")

        await self.check(
            "analysis.ws_status_snapshot", ws_snapshot,
            requires=("analysis.create", ))
        await self.check(
            "analysis.ws_final_event", ws_final,
            requires=("analysis.create", ))

        async def notes_ok() -> str:
            status = _need(final.get("status"), "a finished analysis")
            wanted = ((
                "analysis_started", "analysis_completed",
                "recommendation_ready") if status == "completed" else
                      ("analysis_started", "analysis_failed"))
            for ntype in wanted:
                await self.wait_notification(self.user("alice"), ntype)
            return ", ".join(wanted)

        await self.check(
            "analysis.notifications", notes_ok, requires=("analysis.create", ))

        rec: dict[str, Any] = {}

        async def recommendation() -> str:
            resp = await self.req(
                "GET", f"/api/v1/analyses/{analysis_id}/recommendation",
                who=self.user("alice"))
            self.expect(resp, 200)
            rec.update(_data(resp))
            return (
                f"{rec.get('recommendation')} "
                f"confidence={rec.get('confidence')} "
                f"tiers={len(rec.get('tiers') or [])}")

        await self.check(
            "analysis.recommendation", recommendation,
            requires=("analysis.completed", ))

        tier_checks = (
            "analysis.decision_followed", "analysis.decision_notification",
            "analysis.delete_portfolio", "analysis.tiers_closed",
            "analysis.tier_cancelled_notifications")
        if not self.passed("analysis.recommendation"):
            self.skip_all(tier_checks, "needs analysis.recommendation")
            return
        tiers = rec.get("tiers") or []
        if not tiers:
            self.skip_all(
                tier_checks, f"recommendation {rec.get('recommendation')} "
                "has no tiers (HOLD or not sized); the tier chain is "
                "covered deterministically by ai-service's "
                "scripts.e2e_tier_check")
            return

        async def followed() -> str:
            resp = await self.req(
                "POST", f"/api/v1/analyses/{analysis_id}/decisions",
                who=self.user("alice"), json_body={
                    "action_taken": "followed",
                    "portfolio_id": self.portfolio_id,
                    "notes": f"{self.prefix} decision"
                })
            self.expect(resp, 201)
            return f"decision {_data(resp).get('id')}"

        await self.check("analysis.decision_followed", followed)

        async def decision_note() -> str:
            await self.wait_notification(
                self.user("alice"), "decision_recorded")
            return "decision_recorded"

        await self.check(
            "analysis.decision_notification", decision_note,
            requires=("analysis.decision_followed", ))

        async def delete_portfolio() -> str:
            resp = await self.req(
                "DELETE", f"/api/v1/portfolios/{self.portfolio_id}",
                who=self.user("alice"))
            self.expect(resp, 204)
            self.untrack("portfolio", str(self.portfolio_id))
            return "204 (portfolio.deleted is durable)"

        await self.check("analysis.delete_portfolio", delete_portfolio)
        untriggered = [t for t in tiers if t.get("status") != "triggered"]

        async def tiers_closed() -> str:

            async def attempt() -> dict[str, Any] | None:
                resp = await self.req(
                    "GET", f"/api/v1/analyses/{analysis_id}/recommendation",
                    who=self.user("alice"))
                self.expect(resp, 200)
                body: dict[str, Any] = _data(resp)
                open_ = [
                    t for t in body.get("tiers") or [] if t.get("status") in (
                        "pending_acceptance", "accepted_awaiting_trigger")
                ]
                return None if open_ else body

            body = await poll(attempt, 30.0, 2.0)
            if body is None:
                raise CheckFailed(
                    "untriggered tiers still open 30s after the delete")
            statuses = sorted(
                {str(t.get("status"))
                 for t in body.get("tiers") or []})
            note = str(body.get("tier_generation_note") or "")
            if "portfolio_deleted" not in note:
                raise CheckFailed(
                    f"tier_generation_note lacks portfolio_deleted: "
                    f"{note[-120:]!r}")
            return f"tier statuses {statuses}; note names portfolio_deleted"

        await self.check(
            "analysis.tiers_closed", tiers_closed,
            requires=("analysis.delete_portfolio", ))

        async def cancelled_notes() -> str:
            found = await self.wait_notification(
                self.user("alice"), "tier_cancelled",
                contains="portfolio_deleted", count=len(untriggered),
                timeout=30.0)
            return f"{len(found)} tier_cancelled (portfolio_deleted)"

        await self.check(
            "analysis.tier_cancelled_notifications", cancelled_notes,
            requires=("analysis.delete_portfolio", ))

    async def _analysis_status(self, analysis_id: str) -> str:
        resp = await self.req(
            "GET", f"/api/v1/analyses/{analysis_id}", who=self.user("alice"))
        self.expect(resp, 200)
        return str(_data(resp)["status"])

    async def s_admin(self) -> None:

        async def search() -> str:
            resp = await self.req(
                "GET", "/api/v1/admin/users", who=ADMIN, params={
                    "q": f"e2e-{self.run_id}-",
                    "page_size": 100
                })
            self.expect(resp, 200)
            body = _data(resp)
            found = {i["email"] for i in body.get("items") or []}
            expected = {
                a.email
                for a in self.users.values() if a.user_id and not a.deleted
            }
            if not expected <= found:
                raise CheckFailed(f"missing {sorted(expected - found)}")
            return (
                f"found {len(found)} e2e user(s) (total={body.get('total')})")

        await self.check(
            "admin.user_search", search,
            requires=("auth.admin_token", "auth.login_alice"))

        async def stats() -> str:
            base = _need(self.stats_baseline, "the stats baseline")
            resp = await self.req("GET", "/api/v1/admin/stats", who=ADMIN)
            self.expect(resp, 200)
            now = _data(resp)
            live = list(self.created)
            expected = {
                ("auth", "users_ever"): sum(
                    1 for a in self.users.values() if a.user_id),
                ("core", "portfolios"): sum(
                    1 for c in live if c.kind == "portfolio"),
                ("core", "notes"): sum(1 for c in live if c.kind == "note"),
                ("ai", "backtests"): sum(
                    1 for c in live if c.kind == "backtest")
            }
            parts = []
            for (part, key), minimum in expected.items():
                before, after = stat_value(base, part,
                                           key), stat_value(now, part, key)
                if before is None or after is None:
                    parts.append(f"{part}.{key}=n/a")
                    continue
                if after - before < minimum:
                    raise CheckFailed(
                        f"{part}.{key}: {before} -> {after}, expected "
                        f"+{minimum} or more")
                parts.append(f"{part}.{key} +{after - before}")
            return ", ".join(parts)

        await self.check(
            "admin.site_stats_increased", stats,
            requires=("auth.admin_stats", ))

        gina = self.users["gina"]

        async def delete_gina() -> str:
            resp = await self.req(
                "DELETE", f"/api/v1/admin/users/{_need(gina.user_id, 'gina')}",
                who=ADMIN)
            self.expect(resp, 204)
            gina.deleted = True
            return "204 (soft delete)"

        await self.check(
            "admin.soft_delete_gina", delete_gina,
            requires=("auth.login_gina", "auth.admin_token"))

        async def gina_token() -> str:
            resp = await self.req(
                "GET", "/api/v1/portfolios", token=gina.access)
            self.expect(resp, 401)
            return "existing token -> 401"

        async def gina_login() -> str:
            resp = await self.login(gina)
            self.expect(resp, 401)
            return "login -> 401"

        await self.check(
            "admin.deleted_token_401", gina_token,
            requires=("admin.soft_delete_gina", ))
        await self.check(
            "admin.deleted_login_fails", gina_login,
            requires=("admin.soft_delete_gina", ))

    async def s_cleanup(self) -> None:
        if self.args.keep:
            kept = (
                ", ".join(f"{c.kind}:{c.id}" for c in self.created)
                or "nothing")
            self.record("cleanup.all", SKIP, f"--keep: kept {kept}")
            return
        if not self.created:
            self.record("cleanup.all", OK, "nothing was created")
            return

        async def ensure_active(name: str) -> None:
            actor = self.users[name]
            if actor.deleted or actor.user_id is None or not self.admin_token:
                return
            resp = await self.req(
                "GET", f"/api/v1/admin/users/{actor.user_id}", who=ADMIN)
            if resp.status_code == 200 and not _data(resp).get("is_active"):
                await self.req(
                    "POST", f"/api/v1/admin/users/{actor.user_id}/"
                    "reactivate", who=ADMIN)
                await self.login_ok(actor)

        async def remove(
            kind: str, path_for: Callable[[Created], str],
            codes: tuple[int, ...] = (204, 404)
        ) -> str:
            items = [c for c in self.created if c.kind == kind]
            if not items:
                raise SkipCheck(f"no {kind} left to remove")
            done, errors = 0, []
            for item in items:
                try:
                    await ensure_active(item.owner)
                    resp = await self.req(
                        "DELETE", path_for(item), who=self.users[item.owner])
                    self.expect(resp, *codes)
                    self.untrack(kind, item.id)
                    done += 1
                except (CheckFailed, SkipCheck, httpx.HTTPError) as exc:
                    errors.append(f"{item.id}: {exc}")
            if errors:
                raise CheckFailed(
                    f"{done} removed; failed: {'; '.join(errors)}")
            return f"{done} {kind}(s) removed"

        steps: list[tuple[str, str, Callable[[Created], str]]] = [
            (
                "documents", "document",
                lambda c: f"/api/v1/research/documents/{c.id}"),
            ("notes", "note", lambda c: f"/api/v1/research/notes/{c.id}"),
            (
                "watchlists", "watchlist",
                lambda c: f"/api/v1/watchlists/{c.id}"),
            ("api_keys", "api_key", lambda c: f"/api/v1/api-keys/{c.id}"),
            (
                "portfolios", "portfolio",
                lambda c: f"/api/v1/portfolios/{c.id}"),
            (
                "organizations", "organization",
                lambda c: f"/api/v1/organizations/{c.id}")
        ]
        for label, kind, path_for in steps:
            if any(c.kind == kind for c in self.created):
                await self.check(
                    f"cleanup.{label}",
                    functools.partial(remove, kind, path_for))

        if any(c.kind == "backtest" for c in self.created):
            await self.check(
                "cleanup.backtests",
                functools.partial(
                    remove, "backtest", lambda c: f"/api/v1/backtests/{c.id}"))

        async def users() -> str:
            if not self.admin_token:
                raise CheckFailed("no admin token: test users stay")
            removed, errors = [], []
            for created in [c for c in self.created if c.kind == "user"]:
                actor = self.users[created.owner]
                user_id = created.extra.get("user_id") or actor.user_id
                if user_id is None:
                    errors.append(f"{actor.email}: id unknown (not logged in)")
                    continue
                if not actor.deleted:
                    resp = await self.req(
                        "DELETE", f"/api/v1/admin/users/{user_id}", who=ADMIN)
                    if resp.status_code not in (204, 409):
                        errors.append(f"{actor.name}: HTTP {resp.status_code}")
                        continue
                    actor.deleted = True
                self.untrack("user", created.id)
                removed.append(actor.name)
            if errors:
                raise CheckFailed(f"soft-deleted {removed}; failed: {errors}")
            return f"soft-deleted {', '.join(removed)}"

        await self.check("cleanup.users", users)
        self._print_analysis_cleanup()

    def _print_analysis_cleanup(self) -> None:
        if not self.analysis_ids:
            return
        ids = " ".join(f"--id {aid}" for aid in self.analysis_ids)
        base = (
            "docker compose --env-file .env.docker exec -T ai-service "
            f"python -m scripts.delete_analyses {ids}")
        self.info(
            "the test analysis stays in ai-service; delete it "
            "(preview, then apply):")
        self.info(base)
        self.info(f"{base} --confirm")

    async def run(self, sections: Sequence[str]) -> None:
        for section in sections:
            func = getattr(self, f"s_{section}")
            try:
                await func()
            except Exception as exc:
                self.record(
                    f"{section}.error", FAIL, f"{type(exc).__name__}: {exc}")

    def summary(self, seconds: float) -> str:
        counts = dict.fromkeys((OK, FAIL, SKIP), 0)
        for result in self.results.values():
            counts[result.status] += 1
        line = (
            f"SUMMARY ok={counts[OK]} fail={counts[FAIL]} skip={counts[SKIP]} "
            f"total={len(self.results)} in {seconds:.1f}s")
        failed = [r.name for r in self.results.values() if r.status == FAIL]
        if failed:
            line += " -- failed: " + ", ".join(failed)
        return line


def stat_value(stats: Mapping[str, Any], part: str, key: str) -> int | None:
    section = stats.get(part) or {}
    if part == "auth" and key == "users_ever":
        total, deleted = section.get("users_total"), section.get(
            "users_deleted")
        if total is None or deleted is None:
            return None
        return int(total) + int(deleted)
    value = section.get(key)
    return None if value is None else int(value)


async def poll(
        fn: Callable[[], Awaitable[T | None]], timeout: float,
        interval: float) -> T | None:
    deadline = time.monotonic() + timeout
    while True:
        value = await fn()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(interval)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.e2e_check",
        description="End-to-end check of the running AlphaMind stack through "
        "the gateway (creates and then removes its own test data).",
        epilog=ADMIN_TOKEN_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--with-analysis", action="store_true",
        help="also run a full LLM analysis of AMD (up to 15 min)")
    parser.add_argument(
        "--keep", action="store_true",
        help="keep the test data (skip the cleanup section)")
    parser.add_argument(
        "--only", action="append", default=None, metavar="SECTION[,SECTION]",
        help="run only these sections (their prerequisites are "
        "added; cleanup always runs)")
    parser.add_argument(
        "--verbose", action="store_true",
        help="print every HTTP request (method, path, status)")
    parser.add_argument(
        "--break-lock", action="store_true",
        help="remove the lock of an earlier run that was killed "
        "(only one e2e_check / e2e_tier_check runs at a time)")
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    parser.add_argument(
        "--mailhog-url", default=DEFAULT_MAILHOG_URL, help=argparse.SUPPRESS)
    parser.add_argument(
        "--insecure-host-header", default=DEFAULT_HOST_HEADER,
        help=argparse.SUPPRESS)
    parser.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.only:
        args.only = [
            s.strip() for chunk in args.only for s in chunk.split(",")
            if s.strip()
        ]
    return args


async def _amain(
        args: argparse.Namespace, sections: Sequence[str], out: TextIO) -> int:
    from config import settings
    import shared.redis as shared_redis

    runner = E2E(args, out)
    if runner.admin_token:
        problem = await runner.admin_token_problem()
        if problem is not None:
            await runner.close()
            print(
                f"error: E2E_ADMIN_TOKEN is not usable: {problem} (make a new "
                "token, see --help); nothing was created", file=sys.stderr)
            return 2
    now = datetime.now(UTC).isoformat(timespec="seconds")
    holder = f"e2e_check {runner.run_id} {now}"
    try:
        other = await shared_redis.acquire_e2e_lock(
            settings, holder, break_lock=args.break_lock)
    except Exception as exc:
        other = None
        runner.info(
            f"run lock unavailable ({type(exc).__name__}: {exc}); continuing")
    if other is not None:
        await runner.close()
        print(
            f"error: another end-to-end run is in progress ({other}); wait "
            f"for it, "
            "or pass --break-lock if it was killed", file=sys.stderr)
        return 2
    started = time.monotonic()
    runner.info(
        f"run id {runner.run_id}; base {runner.base_url}; "
        f"sections: {', '.join(sections)}")
    try:
        await runner.run(sections)
    finally:
        with contextlib.suppress(Exception):
            await shared_redis.release_e2e_lock(settings, holder)
        await runner.close()
    print(runner.summary(time.monotonic() - started), file=out, flush=True)
    return 1 if any(r.status == FAIL for r in runner.results.values()) else 0


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    args = parse_args(argv)
    stream = out if out is not None else sys.stdout
    try:
        sections, added = resolve_sections(args.only)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not os.environ.get("E2E_ADMIN_TOKEN", "").strip() and sections != [
            "platform", "cleanup"
    ]:
        print(
            "error: E2E_ADMIN_TOKEN is not set (see --help)", file=sys.stderr)
        return 2
    if added:
        print(
            f"INFO --only: prerequisites added: {', '.join(added)}",
            file=stream)
    import logging
    import warnings

    logging.getLogger().setLevel(logging.WARNING)
    warnings.filterwarnings("ignore")
    return asyncio.run(_amain(args, sections, stream))


if __name__ == "__main__":
    sys.exit(main())
