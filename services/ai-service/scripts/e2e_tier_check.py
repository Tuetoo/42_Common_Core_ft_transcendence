import argparse
import asyncio
import contextlib
import json
import os
import secrets
import ssl
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TextIO
from uuid import UUID
import httpx
from scripts._text import flatten

OK = "OK"
FAIL = "FAIL"
SKIP = "SKIP"

TICKER = "AMD"
DEFAULT_BASE_URL = "https://gateway"
DEFAULT_HOST_HEADER = "localhost"
BUY_QUANTITY = Decimal("10")
BUY_PRICE = Decimal("100")
SELL_FILL_PRICE = Decimal("120")
UNREACHABLE_TARGET = Decimal("99999")
EXECUTED_QUANTITY = Decimal("3")
REJECTED_QUANTITY = Decimal("100000")
CANCELLED_QUANTITY = Decimal("2")
CHECK_TIMEOUT = 60.0


class CheckFailed(Exception):
    pass


class SkipCheck(Exception):
    pass


@dataclass
class Result:
    name: str
    status: str
    detail: str


def _data(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("data") if isinstance(body, dict) else None


def _items(resp: httpx.Response) -> list[dict[str, Any]]:
    try:
        body = resp.json()
    except ValueError:
        return []
    items = body.get("items") if isinstance(body, dict) else None
    return [i for i in items or [] if isinstance(i, dict)]


class TierCheck:

    def __init__(self, args: argparse.Namespace, out: TextIO) -> None:
        self.args = args
        self.out = out
        self.run_id = secrets.token_hex(3)
        self.token = os.environ.get("E2E_ADMIN_TOKEN", "").strip()
        self.results: list[Result] = []
        self.started_at = datetime.now(UTC)
        self.admin_id: UUID | None = None
        self.portfolio_id: str | None = None
        self.portfolio_deleted = False
        self.analysis_id: UUID | None = None
        self.recommendation_id: UUID | None = None
        self.tier_ids: dict[str, UUID] = {}
        self.ws_events: list[dict[str, Any]] = []
        self.ws_error: str | None = None
        base = args.base_url.rstrip("/")
        self.ws_base = (
            "wss" + base[5:] if base.startswith("https") else "ws" + base[4:])
        headers = {"Host": args.host_header} if args.host_header else {}
        self.api = httpx.AsyncClient(
            base_url=base, verify=False, headers=headers, timeout=30.0,
            follow_redirects=False)

    def record(self, name: str, status: str, detail: str) -> None:
        detail = flatten(
            detail.replace(self.token, "<token>") if self.token else detail)
        self.results.append(Result(name, status, detail))
        print(f"{status:<4} tiers.{name}  {detail}", file=self.out, flush=True)

    def passed(self, name: str) -> bool:
        return any(r.name == name and r.status == OK for r in self.results)

    async def check(
            self, name: str, fn: Callable[[], Awaitable[str]], *,
            requires: Sequence[str] = (),
            timeout: float = CHECK_TIMEOUT) -> bool:
        missing = [r for r in requires if not self.passed(r)]
        if missing:
            self.record(name, SKIP, f"needs {', '.join(missing)}")
            return False
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
        self.record(name, status, detail)
        return status == OK

    async def req(
            self, method: str, path: str, *, json_body: Any = None,
            params: Mapping[str, Any] | None = None) -> httpx.Response:
        started = time.monotonic()
        resp = await self.api.request(
            method, path, json=json_body, params=params,
            headers={"Authorization": f"Bearer {self.token}"})
        if self.args.verbose:
            print(
                f"     {method} {path} -> {resp.status_code} "
                f"({time.monotonic() - started:.2f}s)", file=self.out,
                flush=True)
        return resp

    @staticmethod
    def expect(resp: httpx.Response, *codes: int) -> None:
        if resp.status_code not in codes:
            wanted = "/".join(str(c) for c in codes)
            raise CheckFailed(
                f"HTTP {resp.status_code} (expected {wanted}): "
                f"{flatten(resp.text, 220)}")

    async def wait_notification(
            self, ntype: str, contains: str,
            timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        types: set[str] = set()
        while True:
            resp = await self.req(
                "GET", "/api/v1/notifications", params={"page_size": 100})
            self.expect(resp, 200)
            for item in _items(resp):
                created = datetime.fromisoformat(str(item.get("created_at")))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if created < self.started_at:
                    continue
                types.add(str(item.get("type")))
                text = f"{item.get('title')} {item.get('content')}"
                if item.get("type") == ntype and contains in text:
                    return item
            if time.monotonic() > deadline:
                raise CheckFailed(
                    f"no '{ntype}' notification containing {contains!r} "
                    f"after {timeout:g}s; types of this run: "
                    f"{', '.join(sorted(types)) or 'none'}")
            await asyncio.sleep(1.0)

    async def listen(self, stop: asyncio.Event) -> None:
        from websockets.asyncio.client import connect

        ctx: ssl.SSLContext | None = None
        if self.ws_base.startswith("wss"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        url = (
            f"{self.ws_base}/ws/analysis:{self.analysis_id}?token={self.token}"
        )
        try:
            async with connect(url, ssl=ctx, open_timeout=15) as ws:
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    with contextlib.suppress(ValueError):
                        self.ws_events.append(json.loads(raw))
        except Exception as exc:
            self.ws_error = f"{type(exc).__name__}: {exc}"

    def ws_names(self) -> list[str]:
        return [str(e.get("event")) for e in self.ws_events]

    async def tier_rows(self) -> dict[str, tuple[str, str]]:
        from database import SessionLocal
        from sqlalchemy import select
        from domains.analysis.model import Recommendation, RecommendationTier

        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(
                        RecommendationTier.id, RecommendationTier.status,
                        Recommendation.tier_generation_note).join(
                            Recommendation, Recommendation.id ==
                            RecommendationTier.recommendation_id).where(
                                RecommendationTier.id.in_(
                                    list(self.tier_ids.values()))))).all()
        by_id = {row[0]: (str(row[1]), str(row[2] or "")) for row in rows}
        return {
            label: by_id[tid]
            for label, tid in self.tier_ids.items() if tid in by_id
        }

    async def execute(self, label: str, *, again: bool = False) -> str:
        from ai.workflows.tier_monitoring_workflow import (
            _execute_and_mark_triggered, _send_order, _tier_outcome_event)
        from ai_events import publish_event
        from clients.core_client import record_transaction
        from database import SessionLocal
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from domains.analysis.model import Recommendation, RecommendationTier

        async with SessionLocal() as session:
            tier = (
                await session.execute(
                    select(RecommendationTier).where(
                        RecommendationTier.id == self.tier_ids[label]).options(
                            selectinload(RecommendationTier.recommendation).
                            selectinload(Recommendation.analysis))
                )).scalar_one()
            if again:
                await _send_order(
                    record_transaction, tier,
                    portfolio_id=str(self.portfolio_id), ticker=TICKER,
                    price=SELL_FILL_PRICE, transaction_type="sell")
                return "sent again"
            outcome = await _execute_and_mark_triggered(
                tier, ticker=TICKER, price=SELL_FILL_PRICE,
                transaction_type="sell")
            event = _tier_outcome_event(tier, outcome)
            await session.commit()
        if event is not None:
            await publish_event(*event)
        return outcome

    async def run(self) -> None:
        await self.check("admin", self.s_admin)
        await self.check("portfolio", self.s_portfolio, requires=("admin", ))
        await self.check(
            "seed_recommendation", self.s_seed, requires=("portfolio", ))
        stop = asyncio.Event()
        listener: asyncio.Task[None] | None = None
        if self.passed("seed_recommendation"):
            listener = asyncio.create_task(self.listen(stop))
        try:
            await self.steps()
        finally:
            stop.set()
            if listener is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(listener, timeout=10.0)
        await self.check(
            "ws_events", self.s_ws_events, requires=("seed_recommendation", ))
        if self.args.keep:
            self.record(
                "cleanup", SKIP,
                f"--keep: analysis {self.analysis_id}, portfolio "
                f"{self.portfolio_id}")
        else:
            await self.check("cleanup", self.s_cleanup)

    async def steps(self) -> None:
        await self.check(
            "ws_status_snapshot", self.s_ws_snapshot,
            requires=("seed_recommendation", ))
        await self.check(
            "decision_followed", self.s_decision,
            requires=("seed_recommendation", ))
        await self.check(
            "tiers_accepted", self.s_accepted,
            requires=("decision_followed", ))
        await self.check(
            "decision_notification",
            lambda: self.notified("decision_recorded", TICKER),
            requires=("decision_followed", ))
        await self.check(
            "sell_executed", self.s_sell, requires=("tiers_accepted", ))
        await self.check(
            "holdings_after_sell", self.s_holdings,
            requires=("sell_executed", ))
        await self.check(
            "order_idempotent", self.s_idempotent,
            requires=("sell_executed", ))
        await self.check(
            "executed_notification", lambda: self.notified(
                "tier_executed", f"sell {EXECUTED_QUANTITY} {TICKER}"),
            requires=("sell_executed", ))
        await self.check(
            "sell_rejected", self.s_rejected, requires=("tiers_accepted", ))
        await self.check(
            "rejected_notification", lambda: self.notified(
                "tier_execution_failed", str(self.tier_ids.get("rejected"))),
            requires=("sell_rejected", ))
        await self.check(
            "delete_portfolio", self.s_delete_portfolio,
            requires=("portfolio", ))
        await self.check(
            "tier_cancelled", self.s_cancelled,
            requires=("delete_portfolio", "tiers_accepted"))
        await self.check(
            "cancelled_notification",
            lambda: self.notified("tier_cancelled", "portfolio_deleted"),
            requires=("tier_cancelled", ))

    async def s_admin(self) -> str:
        if not self.token:
            raise CheckFailed(
                "E2E_ADMIN_TOKEN is not set (see scripts.e2e_check --help)")
        resp = await self.req("GET", "/api/v1/auth/me")
        self.expect(resp, 200)
        me = _data(resp)
        if me.get("system_role") != "admin":
            raise CheckFailed(f"token user has role {me.get('system_role')!r}")
        self.admin_id = UUID(str(me["id"]))
        return (
            f"admin {me.get('username')} ({self.admin_id}), run {self.run_id}")

    async def s_portfolio(self) -> str:
        resp = await self.req(
            "POST", "/api/v1/portfolios", json_body={
                "name": f"E2E {self.run_id} tiers",
                "currency": "USD"
            })
        self.expect(resp, 201)
        self.portfolio_id = str(_data(resp)["id"])
        base = f"/api/v1/portfolios/{self.portfolio_id}/transactions"
        now = datetime.now(UTC).isoformat()
        for body in ({"ticker": "CASH", "transaction_type": "deposit",
                      "quantity": "10000"},
                     {"ticker": TICKER, "transaction_type": "buy",
                      "quantity": str(BUY_QUANTITY), "price": str(BUY_PRICE)}):
            resp = await self.req(
                "POST", base, json_body={
                    **body, "transaction_date": now
                })
            self.expect(resp, 201)
        return (
            f"portfolio {self.portfolio_id}: deposit 10000, buy "
            f"{BUY_QUANTITY} {TICKER}")

    async def s_seed(self) -> str:
        from database import SessionLocal
        from domains.analysis.model import (Analysis, Recommendation,
                                            RecommendationTier)
        from shared.enums import (RecommendationAction, TierSide, TierStatus,
                                  WorkflowStatus)

        assert self.portfolio_id is not None
        async with SessionLocal() as session:
            analysis = Analysis(
                user_id=self.admin_id, portfolio_id=UUID(self.portfolio_id),
                ticker=TICKER, status=WorkflowStatus.COMPLETED.value)
            session.add(analysis)
            await session.flush()
            recommendation = Recommendation(
                analysis_id=analysis.id,
                recommendation=RecommendationAction.SELL.value, confidence=0.9,
                reasoning=(
                    f"E2E {self.run_id}: synthetic recommendation written by "
                    "scripts.e2e_tier_check; not an analysis result."),
                window_expires_at=datetime.now(UTC) + timedelta(days=3))
            session.add(recommendation)
            await session.flush()
            for label, quantity in (("executed", EXECUTED_QUANTITY),
                                    ("rejected", REJECTED_QUANTITY),
                                    ("cancelled", CANCELLED_QUANTITY)):
                tier = RecommendationTier(
                    recommendation_id=recommendation.id,
                    side=TierSide.SELL.value, target_price=UNREACHABLE_TARGET,
                    quantity=quantity,
                    status=TierStatus.PENDING_ACCEPTANCE.value)
                session.add(tier)
                await session.flush()
                self.tier_ids[label] = tier.id
            await session.commit()
            self.analysis_id = analysis.id
            self.recommendation_id = recommendation.id
        return (
            f"analysis {self.analysis_id}, SELL tiers of {EXECUTED_QUANTITY}/"
            f"{REJECTED_QUANTITY}/{CANCELLED_QUANTITY} shares")

    async def s_ws_snapshot(self) -> str:
        deadline = time.monotonic() + 15
        while (not self.ws_events and time.monotonic() < deadline
               and not self.ws_error):
            await asyncio.sleep(0.2)
        if not self.ws_events:
            raise CheckFailed(
                f"no message on analysis:<id> within 15s {self.ws_error or ''}"
            )
        first = self.ws_events[0]
        payload = first.get("payload") or {}
        if (first.get("event") != "analysis.status"
                or payload.get("status") != "completed"):
            raise CheckFailed(
                f"first message {flatten(json.dumps(first), 200)}")
        return "first message: analysis.status completed"

    async def s_decision(self) -> str:
        resp = await self.req(
            "POST", f"/api/v1/analyses/{self.analysis_id}/decisions",
            json_body={
                "action_taken": "followed",
                "portfolio_id": self.portfolio_id,
                "notes": f"E2E {self.run_id} tiers"
            })
        self.expect(resp, 201)
        return f"decision {_data(resp).get('id')}"

    async def s_accepted(self) -> str:
        rows = await self.tier_rows()
        statuses = {label: status for label, (status, _) in rows.items()}
        if (set(statuses.values()) != {"accepted_awaiting_trigger"}
                or len(statuses) != 3):
            raise CheckFailed(f"tier statuses {statuses}")
        return "3 tiers accepted_awaiting_trigger"

    async def notified(self, ntype: str, contains: str) -> str:
        item = await self.wait_notification(ntype, contains)
        return f"{ntype}: {item.get('content')}"

    async def s_sell(self) -> str:
        outcome = await self.execute("executed")
        status = (await self.tier_rows())["executed"][0]
        if outcome != "triggered" or status != "triggered":
            raise CheckFailed(f"outcome {outcome!r}, tier status {status!r}")
        return (
            f"sold {EXECUTED_QUANTITY} {TICKER} at {SELL_FILL_PRICE}: tier "
            f"triggered")

    async def sells(self) -> list[dict[str, Any]]:
        resp = await self.req(
            "GET", f"/api/v1/portfolios/{self.portfolio_id}/transactions",
            params={"page_size": 100})
        self.expect(resp, 200)
        return [t for t in _items(resp) if t.get("transaction_type") == "sell"]

    async def s_holdings(self) -> str:
        resp = await self.req(
            "GET", f"/api/v1/portfolios/{self.portfolio_id}/holdings")
        self.expect(resp, 200)
        holding = next(
            (h for h in _data(resp) or [] if h.get("ticker") == TICKER), None)
        expected = BUY_QUANTITY - EXECUTED_QUANTITY
        if holding is None or Decimal(str(holding["quantity"])) != expected:
            raise CheckFailed(
                f"{TICKER} holding {holding}, expected {expected}")
        sells = await self.sells()
        if len(sells) != 1:
            raise CheckFailed(f"{len(sells)} sell transactions, expected 1")
        return f"{TICKER} {expected} after the sell; 1 sell in the ledger"

    async def s_idempotent(self) -> str:
        await self.execute("executed", again=True)
        sells = await self.sells()
        if len(sells) != 1:
            raise CheckFailed(
                f"{len(sells)} sell transactions after a repeated order")
        return "the repeated order (same idempotency key) did not trade again"

    async def s_rejected(self) -> str:
        outcome = await self.execute("rejected")
        status, note = (await self.tier_rows())["rejected"]
        if outcome != "execution_rejected" or status != "execution_failed":
            raise CheckFailed(f"outcome {outcome!r}, tier status {status!r}")
        line = next((
            ln for ln in note.splitlines()
            if str(self.tier_ids["rejected"]) in ln), "")
        if "refused the order" not in line:
            raise CheckFailed(
                f"recommendation note lacks the refusal: {note[-160:]!r}")
        return f"core refused {REJECTED_QUANTITY} shares: {line}"

    async def s_delete_portfolio(self) -> str:
        resp = await self.req(
            "DELETE", f"/api/v1/portfolios/{self.portfolio_id}")
        self.expect(resp, 204)
        self.portfolio_deleted = True
        return "204"

    async def s_cancelled(self) -> str:
        deadline = time.monotonic() + 30
        while True:
            rows = await self.tier_rows()
            status, note = rows["cancelled"]
            if status not in ("pending_acceptance",
                              "accepted_awaiting_trigger"):
                break
            if time.monotonic() > deadline:
                raise CheckFailed(
                    "the remaining tier is still open 30s after the delete")
            await asyncio.sleep(1.0)
        if "portfolio_deleted" not in note:
            raise CheckFailed(f"note lacks portfolio_deleted: {note[-160:]!r}")
        others = {label: rows[label][0] for label in ("executed", "rejected")}
        if others != {"executed": "triggered", "rejected": "execution_failed"}:
            raise CheckFailed(f"finished tiers changed: {others}")
        return (
            f"remaining tier {status} (portfolio_deleted); finished tiers "
            f"unchanged")

    async def s_ws_events(self) -> str:
        names = self.ws_names()
        wanted = [
            "analysis.status", "decision.recorded", "tier.executed",
            "tier.execution_failed", "tier.cancelled"
        ]
        missing = [name for name in wanted if name not in names]
        if missing:
            raise CheckFailed(
                f"missing {missing}; seen {names} {self.ws_error or ''}")
        return f"events: {', '.join(dict.fromkeys(names))}"

    async def s_cleanup(self) -> str:
        from database import SessionLocal
        from sqlalchemy import text

        done: list[str] = []
        if self.portfolio_id and not self.portfolio_deleted:
            resp = await self.req(
                "DELETE", f"/api/v1/portfolios/{self.portfolio_id}")
            self.expect(resp, 204, 404)
            done.append("portfolio")
        if self.analysis_id is None:
            return ", ".join(done) or "nothing was created"
        rec = {"rec": self.recommendation_id}
        statements = (
            (
                "DELETE FROM ai.decision_outcomes WHERE decision_id IN "
                "(SELECT id FROM "
                "ai.investment_decisions WHERE recommendation_id = :rec)",
                rec),
            (
                "DELETE FROM ai.decision_reviews WHERE decision_id IN (SELECT "
                "id FROM "
                "ai.investment_decisions WHERE recommendation_id = :rec)",
                rec),
            (
                "DELETE FROM ai.investment_decisions WHERE recommendation_id "
                "= :rec", rec), (
                    "DELETE FROM ai.recommendation_metrics WHERE "
                    "recommendation_id = :rec", rec),
            (
                "DELETE FROM ai.recommendation_tiers WHERE recommendation_id "
                "= :rec", rec),
            ("DELETE FROM ai.recommendations WHERE id = :rec", rec), (
                "DELETE FROM ai.analyses WHERE id = :analysis", {
                    "analysis": self.analysis_id
                }))
        try:
            async with SessionLocal() as session:
                for sql, params in statements:
                    await session.execute(text(sql), params)
                await session.commit()
        except Exception as exc:
            raise CheckFailed(
                f"{type(exc).__name__}: {exc}; remove analysis "
                f"{self.analysis_id} with "
                "scripts.delete_analyses --id") from exc
        done.append(
            f"analysis {self.analysis_id} with its recommendation, tiers and "
            f"decision")
        return "removed " + ", ".join(done)

    def summary(self) -> int:
        counts = {
            s: sum(1 for r in self.results if r.status == s)
            for s in (OK, FAIL, SKIP)
        }
        failed = [r.name for r in self.results if r.status == FAIL]
        tail = f" -- failed: {', '.join(failed)}" if failed else ""
        print(
            f"SUMMARY ok={counts[OK]} fail={counts[FAIL]} skip={counts[SKIP]} "
            f"total={len(self.results)}{tail}", file=self.out, flush=True)
        return 1 if failed else 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.e2e_tier_check",
        description=("Deterministic end-to-end check of the tier chain."))
    parser.add_argument(
        "--keep", action="store_true",
        help="keep the synthetic analysis and the portfolio")
    parser.add_argument(
        "--verbose", action="store_true", help="print every request")
    parser.add_argument(
        "--break-lock", action="store_true",
        help="remove the lock of an earlier end-to-end run that was killed")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--host-header", default=DEFAULT_HOST_HEADER)
    return parser.parse_args(argv)


async def main_async(argv: Sequence[str], out: TextIO = sys.stdout) -> int:
    from config import settings
    from shared import e2e_lock

    args = parse_args(argv)
    checker = TierCheck(args, out)
    try:
        await checker.s_admin()
    except Exception as exc:
        await checker.api.aclose()
        print(
            f"error: E2E_ADMIN_TOKEN is not usable: {exc} (make a new token); "
            "nothing was created", file=sys.stderr)
        return 2
    holder = (
        f"e2e_tier_check {checker.run_id} "
        f"{datetime.now(UTC).isoformat(timespec='seconds')}")
    try:
        other = await e2e_lock.acquire(
            settings, holder, break_lock=args.break_lock)
    except Exception as exc:
        other = None
        print(
            f"INFO run lock unavailable ({type(exc).__name__}: {exc}); "
            f"continuing", file=out, flush=True)
    if other is not None:
        await checker.api.aclose()
        print(
            f"error: another end-to-end run is in progress ({other}); wait "
            f"for it, "
            "or pass --break-lock if it was killed", file=sys.stderr)
        return 2
    try:
        await checker.run()
    finally:
        with contextlib.suppress(Exception):
            await e2e_lock.release(settings, holder)
        await checker.api.aclose()
        with contextlib.suppress(Exception):
            from database import engine

            await engine.dispose()
    return checker.summary()


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
