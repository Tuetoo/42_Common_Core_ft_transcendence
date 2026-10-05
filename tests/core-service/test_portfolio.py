import asyncio
import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
import pytest
from helpers import create_portfolio
from shared.user_state import UserState
from tests.auth_tokens import auth_headers as _make_token

SESSION = date(2026, 9, 29)
FUNDING_TIME = datetime(2026, 9, 28, 15, tzinfo=UTC)


@pytest.fixture
def silent_notifications(monkeypatch):
    import domains.notification.actions as actions

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(actions, "notify_users", _noop)


@pytest.fixture
def notified_users(monkeypatch):
    import domains.notification.actions as actions

    calls: list[dict] = []

    async def _record(user_ids, *, type, title, content):
        calls.append({
            "user_ids": [str(u) for u in user_ids if u],
            "type": str(type),
            "title": title,
            "content": content
        })

    monkeypatch.setattr(actions, "notify_users", _record)
    return calls


async def _txn(client, pid, headers, **body):
    body.setdefault("transaction_date", datetime.now(UTC).isoformat())
    return await client.post(
        f"/api/v1/portfolios/{pid}/transactions", json=body, headers=headers)


async def _txn_at(client, pid, headers, when, **body):
    response = await client.post(
        f"/api/v1/portfolios/{pid}/transactions", json={
            "transaction_date": when.isoformat(),
            **body
        }, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _deposit(client, pid, headers, amount):
    response = await _txn(
        client, pid, headers, ticker="CASH", transaction_type="deposit",
        quantity=str(amount))
    assert response.status_code == 201, response.text
    return response


async def _cash(client, pid):
    response = await client.get(f"/internal/portfolios/{pid}/cash-balance")
    assert response.status_code == 200
    return Decimal(str(response.json()["cash_balance"]))


async def _holdings_by_ticker(client, pid, headers):
    response = await client.get(
        f"/api/v1/portfolios/{pid}/holdings", headers=headers)
    assert response.status_code == 200
    return {h["ticker"]: h for h in response.json()["data"]}


async def _ledger(client, pid, headers):
    response = await client.get(
        f"/api/v1/portfolios/{pid}/transactions?page_size=100",
        headers=headers)
    assert response.status_code == 200
    return response.json()["items"]


def _withdrawal(amount, when):
    body = {
        "ticker": "CASH",
        "transaction_type": "withdrawal",
        "transaction_date": when.isoformat()
    }
    if amount is not None:
        body["quantity"] = str(amount)
    return body


async def _enable_auto_analysis(client, headers, *tickers):
    pid = await create_portfolio(client, headers)
    await client.patch(
        f"/api/v1/portfolios/{pid}", json={"auto_analyze_at_open": True},
        headers=headers)
    for ticker in tickers:
        await client.post(
            f"/api/v1/portfolios/{pid}/holdings", json={
                "ticker": ticker,
                "quantity": "1",
                "average_cost": "10"
            }, headers=headers)
    return pid


async def _portfolio_with_positions(client, headers, *tickers):
    pid = UUID(await create_portfolio(client, headers))
    await _txn_at(
        client, pid, headers, FUNDING_TIME, ticker="CASH",
        transaction_type="deposit", quantity="10000")
    for ticker in tickers:
        await _txn_at(
            client, pid, headers, FUNDING_TIME, ticker=ticker,
            transaction_type="buy", quantity="10", price="100")
    return pid


async def _portfolio_with_cash(client, headers):
    pid = await create_portfolio(client, headers)
    await _txn_at(
        client, pid, headers, datetime(2026, 1, 1, 15, tzinfo=UTC),
        ticker="CASH", transaction_type="deposit", quantity="100000")
    return pid


def _only_portfolios(monkeypatch, *pids):
    from domains.portfolio.repository import PortfolioRepository

    real_list_all = PortfolioRepository.list_all
    real_list_all = getattr(real_list_all, "unfiltered", real_list_all)

    async def _list_all(self):
        return [p for p in await real_list_all(self) if p.id in set(pids)]

    _list_all.unfiltered = real_list_all
    monkeypatch.setattr(PortfolioRepository, "list_all", _list_all)


async def _snapshot(db_session, pid, day):
    from domains.portfolio.repository import PortfolioSnapshotRepository

    db_session.expire_all()
    rows = await PortfolioSnapshotRepository(db_session).list_for_portfolio(
        pid)
    return next((r for r in rows if r.snapshot_date == day), None)


async def _replay_matches_stored(db_session, pid):
    from domains.portfolio.ledger import amounts_equal, replay_positions
    from domains.portfolio.repository import (HoldingRepository,
                                              TransactionRepository)

    txns = await TransactionRepository(db_session).list_all_for_portfolio(
        UUID(pid))
    replayed = replay_positions(txns).held()
    stored = await HoldingRepository(db_session).list_for_portfolio(UUID(pid))
    assert set(replayed) == {h.ticker for h in stored}
    for h in stored:
        assert amounts_equal(replayed[h.ticker].quantity, h.quantity)
        assert amounts_equal(replayed[h.ticker].average_cost, h.average_cost)


def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.text)))


async def _no_orgs(user_id):
    return []


@dataclass
class _LedgerRow:
    transaction_type: str
    ticker: str
    quantity: Decimal | None
    transaction_date: datetime
    price: Decimal | None = None
    dividend_per_share: Decimal | None = None
    split_ratio: Decimal | None = None


def _at(day: int, hour: int = 15) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


LEDGER = [
    _LedgerRow("deposit", "CASH", Decimal("10000"), _at(1)),
    _LedgerRow("buy", "AAPL", Decimal("10"), _at(2), price=Decimal("100")),
    _LedgerRow("stock_split", "AAPL", None, _at(3), split_ratio=Decimal("2")),
    _LedgerRow(
        "adjustment", "MSFT", Decimal("5"), _at(3), price=Decimal("50")),
    _LedgerRow("sell", "AAPL", Decimal("4"), _at(4), price=Decimal("60")),
    _LedgerRow(
        "dividend", "MSFT", Decimal("5"), _at(4),
        dividend_per_share=Decimal("1")),
    _LedgerRow("withdrawal", "CASH", Decimal("100"), _at(8))
]


class TestPortfolioAccess:

    async def test_portfolio_listing_creation_and_paging_rules(
            self, client, db_session, monkeypatch):
        from shared.schemas import MAX_PAGE

        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        for url in ("/api/v1/portfolios",
                    f"/api/v1/portfolios/{pid}/transactions"):
            resp = await client.get(
                url, params={"page": MAX_PAGE + 1}, headers=headers)
            assert resp.status_code == 422, url
            ok = await client.get(
                url, params={"page": MAX_PAGE}, headers=headers)
            assert ok.status_code == 200, url

        import clients.auth_client as auth_client
        from domains.portfolio.repository import PortfolioRepository

        org = uuid4()
        other = uuid4()
        repo = PortfolioRepository(db_session)
        shared = await repo.create(
            owner_id=other, name="shared", currency="USD", organization_id=org,
            visibility="organization")
        private = await repo.create(
            owner_id=other, name="private", currency="USD",
            organization_id=org, visibility="private")

        async def _orgs(user_id):
            return [org]

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _orgs)
        _, headers = _make_token()
        resp = await client.get("/api/v1/portfolios", headers=headers)
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(shared.id) in ids
        assert str(private.id) not in ids

        import clients.auth_client as auth_client

        mine = uuid4()
        watched = uuid4()

        async def _orgs(user_id):
            return [mine, watched]

        async def _roles(user_id):
            return {mine: "member", watched: "viewer"}

        monkeypatch.setattr(auth_client, "list_user_organization_ids", _orgs)
        monkeypatch.setattr(
            auth_client, "list_user_organization_roles", _roles)
        _, headers = _make_token()
        viewer = await client.post(
            "/api/v1/portfolios", json={
                "name": "x",
                "organization_id": str(watched),
                "visibility": "organization"
            }, headers=headers)
        assert viewer.status_code == 403
        foreign = await client.post(
            "/api/v1/portfolios", json={
                "name": "x",
                "organization_id": str(uuid4()),
                "visibility": "organization"
            }, headers=headers)
        assert foreign.status_code == 403
        ok = await client.post(
            "/api/v1/portfolios", json={
                "name": "x",
                "organization_id": str(mine),
                "visibility": "organization"
            }, headers=headers)
        assert ok.status_code == 201, ok.text


class TestTransactions:

    async def test_split_dividend_ticker_and_invalid_transactions(
            self, client, mock_cross_service):
        _, headers = _make_token()
        pid = await create_portfolio(client, headers, "Split Test")
        await _deposit(client, pid, headers, "1000")

        buy = await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="buy",
            quantity="10", price="100")
        assert buy.status_code == 201

        split = await _txn(
            client, pid, headers, ticker="AAPL",
            transaction_type="stock_split", split_ratio="2.0")
        assert split.status_code == 201

        holdings = (
            await
            client.get(f"/api/v1/portfolios/{pid}/holdings",
                       headers=headers)).json()["data"]
        assert len(holdings) == 1
        assert Decimal(holdings[0]["quantity"]) == Decimal("20")
        assert Decimal(holdings[0]["average_cost"]) == Decimal("50")

        _, headers = _make_token()
        pid = await create_portfolio(client, headers, "Split Missing Ratio")

        await _deposit(client, pid, headers, "1500")
        buy = await _txn(
            client, pid, headers, ticker="MSFT", transaction_type="buy",
            quantity="5", price="300")
        assert buy.status_code == 201

        response = await _txn(
            client, pid, headers, ticker="MSFT",
            transaction_type="stock_split")
        assert response.status_code == 422

        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        await _deposit(client, pid, headers, "1000")
        buy = await _txn(
            client, pid, headers, ticker=" aapl ", transaction_type="buy",
            quantity="2", price="10")
        assert buy.status_code == 201
        assert buy.json()["data"]["ticker"] == "AAPL"

        held_dividend = await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="dividend",
            quantity="2", dividend_per_share="0.5")
        assert held_dividend.status_code == 201, held_dividend.text
        sell = await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="sell",
            quantity="1", price="12")
        assert sell.status_code == 201, sell.text

        for case_id, body in (("sell-without-price", {
                "ticker": "AAPL", "transaction_type": "sell", "quantity": "1"
        }), ("deposit-without-quantity", {
                "ticker": "CASH", "transaction_type": "deposit"
        }), ("dividend-on-unheld-ticker", {
                "ticker": "MSFT", "transaction_type": "dividend",
                "quantity": "1", "dividend_per_share": "1"
        }), ("buy-cash-ticker",
             {"ticker": "CASH", "transaction_type": "buy", "quantity": "1",
              "price": "1"}), ("ticker-with-space", {
                  "ticker": "AA PL", "transaction_type": "buy",
                  "quantity": "1",
                  "price": "1"
              }), ("ticker-too-long",
                   {"ticker": "TOOLONGTICKER", "transaction_type": "buy",
                    "quantity": "1", "price": "1"}), ("quantity-overflow", {
                        "ticker": "AAPL",
                        "transaction_type": "buy",
                        "quantity": "1e40",
                        "price": "1"
                    }), ("price-nan", {
                        "ticker": "AAPL",
                        "transaction_type": "buy",
                        "quantity": "1",
                        "price": "NaN"
                    }), ("future-date",
                         {"ticker": "AAPL", "transaction_type": "buy",
                          "quantity": "1",
                          "price": "1",
                          "transaction_date": (datetime.now(UTC) +
                                               timedelta(days=3)).isoformat()
                          })):
            _, headers = _make_token()
            pid = await create_portfolio(client, headers)
            await _deposit(client, pid, headers, "1000")
            buy = await _txn(
                client, pid, headers, ticker="AAPL", transaction_type="buy",
                quantity="2", price="10")
            assert buy.status_code == 201, case_id
            resp = await _txn(client, pid, headers, **body)
            assert resp.status_code == 422, (
                case_id, resp.status_code, resp.text)

    async def test_internal_transaction_idempotency_key(self, client):
        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        other_pid = await create_portfolio(client, headers)
        assert (
            await _txn(
                client, pid, headers, ticker="CASH",
                transaction_type="deposit",
                quantity="1000")).status_code == 201
        key = f"tier:{uuid4()}"
        body = {
            "transaction_type": "buy",
            "ticker": "AMD",
            "quantity": 2.0,
            "price": 100.0,
            "transaction_date": datetime.now(UTC).date().isoformat(),
            "idempotency_key": key
        }

        first = await client.post(
            f"/internal/portfolios/{pid}/transactions", json=body)
        assert first.status_code == 200, first.text
        again = await client.post(
            f"/internal/portfolios/{pid}/transactions", json=body)
        assert again.status_code == 200, again.text
        assert again.json() == first.json()

        holdings = (
            await
            client.get(f"/api/v1/portfolios/{pid}/holdings",
                       headers=headers)).json()["data"]
        assert len(holdings) == 1
        assert Decimal(str(holdings[0]["quantity"])) == 2
        listed = (
            await client.get(
                f"/api/v1/portfolios/{pid}/transactions",
                headers=headers)).json()
        assert listed["total"] == 2

        clash = await client.post(
            f"/internal/portfolios/{other_pid}/transactions", json=body)
        assert clash.status_code == 409

        too_long = await client.post(
            f"/internal/portfolios/{pid}/transactions", json={
                **body, "idempotency_key": "x" * 65
            })
        assert too_long.status_code == 422

    async def test_concurrent_withdrawals_cannot_jointly_overdraw_cash(
            self, real_client, mock_cross_service):
        _, headers = _make_token()
        pid = await create_portfolio(
            real_client, headers, name=f"Race {uuid4()}")
        await _deposit(real_client, pid, headers, "1000")

        now = datetime.now(UTC)
        results = await asyncio.gather(
            _txn(real_client, pid, headers, **_withdrawal("600", now)),
            _txn(real_client, pid, headers, **_withdrawal("600", now)),
            return_exceptions=True)
        for r in results:
            assert not isinstance(r, Exception), f"request raised: {r}"
        assert sorted(r.status_code for r in results) == [201, 422]


@pytest.mark.usefixtures("silent_notifications")
class TestInternalOrders:

    async def test_order_of_a_deactivated_owner_is_refused(
            self, client, monkeypatch):
        import api.internal as internal
        from config import settings

        _, headers = _make_token()
        pid = await _portfolio_with_cash(client, headers)
        monkeypatch.setattr(settings, "USER_STATE_CHECK_ENABLED", True)
        states = {"active": False}

        async def _state(_settings, user_id):
            return UserState(role="user", is_active=states["active"])

        monkeypatch.setattr(internal, "get_user_state", _state)
        body = {
            "ticker": "AMD",
            "transaction_type": "buy",
            "quantity": "1",
            "price": "10",
            "transaction_date": "2026-09-30",
            "idempotency_key": f"tier:{uuid4()}"
        }
        refused = await client.post(
            f"/internal/portfolios/{pid}/transactions", json=body)
        assert refused.status_code == 403
        states["active"] = True
        ok = await client.post(
            f"/internal/portfolios/{pid}/transactions", json=body)
        assert ok.status_code in (200, 201), ok.text
        states["active"] = False
        again = await client.post(
            f"/internal/portfolios/{pid}/transactions", json=body)
        assert again.status_code in (200, 201)
        assert again.json()["id"] == ok.json()["id"]

    async def test_tier_style_sell_after_a_same_day_buy(self, real_client):
        client = real_client
        _, headers = _make_token()
        pid = await _portfolio_with_cash(client, headers)
        buy_at = datetime(2026, 9, 30, 15, tzinfo=UTC)
        await _txn_at(
            client, pid, headers, buy_at, ticker="AMD", transaction_type="buy",
            quantity="10", price="100")
        body = {
            "ticker": "AMD",
            "transaction_type": "sell",
            "quantity": "3",
            "price": "120",
            "idempotency_key": f"tier:{uuid4()}"
        }
        early = await client.post(
            f"/internal/portfolios/{pid}/transactions", json={
                **body, "transaction_date": "2026-09-30"
            })
        assert early.status_code == 422
        fill = await client.post(
            f"/internal/portfolios/{pid}/transactions", json={
                **body, "transaction_date": "2026-09-30T21:00:00+00:00"
            })
        assert fill.status_code in (200, 201), fill.text
        holding = (await _holdings_by_ticker(client, pid, headers))["AMD"]
        assert Decimal(holding["quantity"]) == 7


@pytest.mark.usefixtures("silent_notifications")
class TestLedgerAdjustments:

    async def test_adjustment_ledger_math_and_validation(self, client):
        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        assert (
            await _txn(
                client, pid, headers, ticker="CASH",
                transaction_type="deposit",
                quantity="2000")).status_code == 201
        assert (
            await _txn(
                client, pid, headers, ticker="AAPL", transaction_type="buy",
                quantity="10", price="100")).status_code == 201
        assert await _cash(client, pid) == Decimal(1000)

        shares_in = await _txn(
            client, pid, headers, ticker="aapl", transaction_type="adjustment",
            quantity="10", price="200")
        assert shares_in.status_code == 201, shares_in.text
        body = shares_in.json()["data"]
        assert body["transaction_type"] == "adjustment"
        assert Decimal(body["quantity"]) == 10
        holding = (await _holdings_by_ticker(client, pid, headers))["AAPL"]
        assert Decimal(holding["quantity"]) == 20
        assert Decimal(holding["average_cost"]) == 150
        assert await _cash(client, pid) == Decimal(1000)

        shares_out = await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="adjustment",
            quantity="-5")
        assert shares_out.status_code == 201, shares_out.text
        assert Decimal(shares_out.json()["data"]["quantity"]) == -5
        holding = (await _holdings_by_ticker(client, pid, headers))["AAPL"]
        assert Decimal(holding["quantity"]) == 15
        assert Decimal(holding["average_cost"]) == 150
        assert await _cash(client, pid) == Decimal(1000)

        zero = await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="adjustment",
            quantity="5", price="0")
        assert zero.status_code == 201, zero.text
        holding = (await _holdings_by_ticker(client, pid, headers))["AAPL"]
        assert Decimal(holding["average_cost"]) == Decimal("112.5")

        all_out = await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="adjustment",
            quantity="-20")
        assert all_out.status_code == 201
        assert "AAPL" not in await _holdings_by_ticker(client, pid, headers)
        assert await _cash(client, pid) == Decimal(1000)

        for case_id, body in (("adjustment-zero-quantity", {
                "ticker": "AAPL", "transaction_type": "adjustment",
                "quantity": "0", "price": "1"
        }), ("adjustment-without-quantity",
             {"ticker": "AAPL", "transaction_type": "adjustment", "price": "1"
              }), ("adjustment-increase-without-price",
                   {"ticker": "AAPL", "transaction_type": "adjustment",
                    "quantity": "5"}), ("adjustment-negative-price", {
                        "ticker": "AAPL", "transaction_type": "adjustment",
                        "quantity": "5", "price": "-1"
                    }), ("adjustment-with-split-ratio",
                         {"ticker": "AAPL", "transaction_type": "adjustment",
                          "quantity": "5", "price": "1", "split_ratio": "2"}),
                              ("adjustment-on-cash",
                               {"ticker": "CASH",
                                "transaction_type": "adjustment",
                                "quantity": "5", "price": "1"}),
                              ("buy-negative-quantity",
                               {"ticker": "AAPL", "transaction_type": "buy",
                                "quantity": "-5", "price": "1"}),
                              ("buy-zero-price",
                               {"ticker": "AAPL", "transaction_type": "buy",
                                "quantity": "5", "price": "0"
                                }), ("deposit-negative-quantity",
                                     {"ticker": "CASH",
                                      "transaction_type": "deposit",
                                      "quantity": "-5"})):
            _, headers = _make_token()
            pid = await create_portfolio(client, headers)
            response = await _txn(client, pid, headers, **body)
            assert response.status_code == 422, (case_id, response.text)

    async def test_private_holding_endpoints_record_adjustments_for_owner_only(
            self, client, db_session):
        _, headers = _make_token()
        pid = await create_portfolio(client, headers)

        created = await client.post(
            f"/api/v1/portfolios/{pid}/holdings", json={
                "ticker": "msft",
                "quantity": "10",
                "average_cost": "300"
            }, headers=headers)
        assert created.status_code == 201, created.text
        data = created.json()["data"]
        holding_id = data["id"]
        assert data["ticker"] == "MSFT"
        assert Decimal(data["quantity"]) == 10
        assert data["transaction_id"]
        assert data["transaction_ids"] == [data["transaction_id"]]
        ledger = await _ledger(client, pid, headers)
        assert [(
            t["transaction_type"], Decimal(t["quantity"]), Decimal(t["price"]))
                for t in ledger] == [("adjustment", Decimal(10), Decimal(300))]
        assert ledger[0]["id"] == data["transaction_id"]
        assert await _cash(client, pid) == 0

        increased = await client.patch(
            f"/api/v1/portfolios/{pid}/holdings/{holding_id}",
            json={"quantity": "12"}, headers=headers)
        assert increased.status_code == 200, increased.text
        data = increased.json()["data"]
        assert data["id"] == holding_id
        assert Decimal(data["quantity"]) == 12
        assert Decimal(data["average_cost"]) == 300
        assert len(data["transaction_ids"]) == 1

        decreased = await client.patch(
            f"/api/v1/portfolios/{pid}/holdings/{holding_id}",
            json={"quantity": "7"}, headers=headers)
        assert Decimal(decreased.json()["data"]["quantity"]) == 7

        recosted = await client.patch(
            f"/api/v1/portfolios/{pid}/holdings/{holding_id}", json={
                "quantity": "8",
                "average_cost": "250"
            }, headers=headers)
        assert recosted.status_code == 200
        data = recosted.json()["data"]
        assert data["id"] == holding_id
        assert Decimal(data["quantity"]) == 8
        assert Decimal(data["average_cost"]) == 250
        assert len(data["transaction_ids"]) == 2

        same = await client.patch(
            f"/api/v1/portfolios/{pid}/holdings/{holding_id}",
            json={"quantity": "8"}, headers=headers)
        assert same.json()["data"]["transaction_id"] is None
        await _replay_matches_stored(db_session, pid)

        deleted = await client.delete(
            f"/api/v1/portfolios/{pid}/holdings/{holding_id}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        data = deleted.json()["data"]
        assert Decimal(data["quantity"]) == 0 and data["transaction_id"]
        assert await _holdings_by_ticker(client, pid, headers) == {}
        ledger = await _ledger(client, pid, headers)
        assert [Decimal(t["quantity"]) for t in reversed(ledger)] == [
            Decimal(10),
            Decimal(2),
            Decimal(-5),
            Decimal(-7),
            Decimal(8),
            Decimal(-8)
        ]
        assert all(t["transaction_type"] == "adjustment" for t in ledger)
        assert await _cash(client, pid) == 0
        await _replay_matches_stored(db_session, pid)

        _, owner = _make_token()
        _, stranger = _make_token()
        pid = await create_portfolio(client, owner)
        created = (
            await client.post(
                f"/api/v1/portfolios/{pid}/holdings", json={
                    "ticker": "IBM",
                    "quantity": "1",
                    "average_cost": "100"
                }, headers=owner)).json()["data"]
        response = await client.patch(
            f"/api/v1/portfolios/{pid}/holdings/{created['id']}",
            json={"quantity": "2"}, headers=stranger)
        assert response.status_code == 404
        missing = await client.delete(
            f"/api/v1/portfolios/{pid}/holdings/{uuid4()}", headers=owner)
        assert missing.status_code == 404

    async def test_holding_edit_is_dated_after_future_dated_rows(
            self, client, db_session):
        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        now = datetime.now(UTC)
        tomorrow = now + timedelta(hours=20)
        await _txn_at(
            client, pid, headers, now - timedelta(days=2), ticker="CASH",
            transaction_type="deposit", quantity="10000")
        buys = ((now - timedelta(days=1), "10"), (tomorrow, "5"))
        for when, quantity in buys:
            await _txn_at(
                client, pid, headers, when, ticker="AAPL",
                transaction_type="buy", quantity=quantity, price="100")
        holding = (await _holdings_by_ticker(client, pid, headers))["AAPL"]
        assert Decimal(holding["quantity"]) == 15

        recosted = await client.patch(
            f"/api/v1/portfolios/{pid}/holdings/{holding['id']}",
            json={"average_cost": "90"}, headers=headers)
        assert recosted.status_code == 200, recosted.text
        assert Decimal(recosted.json()["data"]["quantity"]) == 15
        ledger = await _ledger(client, pid, headers)
        adjustments = [
            datetime.fromisoformat(t["transaction_date"]) for t in ledger
            if t["transaction_type"] == "adjustment"
        ]
        assert len(adjustments) == 2
        assert all(moment > tomorrow for moment in adjustments)
        await _replay_matches_stored(db_session, pid)

        deleted = await client.delete(
            f"/api/v1/portfolios/{pid}/holdings/{holding['id']}",
            headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert await _holdings_by_ticker(client, pid, headers) == {}
        await _replay_matches_stored(db_session, pid)

    async def test_public_put_holding_records_adjustment(self, real_client):
        client = real_client
        _, headers = _make_token()
        pid = await create_portfolio(client, headers, "Public ledger")
        holding = (
            await client.post(
                f"/api/v1/portfolios/{pid}/holdings", json={
                    "ticker": "AAPL",
                    "quantity": "4",
                    "average_cost": "150"
                }, headers=headers)).json()["data"]
        key = (
            await client.post(
                "/api/v1/api-keys", json={"name": "ledger"},
                headers=headers)).json()["data"]["key"]

        response = await client.put(
            f"/api/v1/public/portfolios/{pid}/holdings/{holding['id']}",
            json={"quantity": "6"}, headers={"X-API-Key": key})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["id"] == holding["id"]
        assert Decimal(data["quantity"]) == 6
        assert Decimal(data["average_cost"]) == 150
        assert data["transaction_id"]

        ledger = await _ledger(client, pid, headers)
        assert ledger[0]["id"] == data["transaction_id"]
        assert ledger[0]["transaction_type"] == "adjustment"
        assert Decimal(ledger[0]["quantity"]) == 2
        assert Decimal(ledger[0]["price"]) == 150

    async def test_internal_cash_flows_include_adjustments(self, client):
        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        day = datetime(2026, 9, 1, 15, tzinfo=UTC)
        await _txn(
            client, pid, headers, ticker="CASH", transaction_type="deposit",
            quantity="1000", transaction_date=day.isoformat())
        await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="buy",
            quantity="1", price="100", transaction_date=day.isoformat())
        await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="adjustment",
            quantity="5", price="120",
            transaction_date=(day + timedelta(days=1)).isoformat())
        await _txn(
            client, pid, headers, ticker="AAPL", transaction_type="adjustment",
            quantity="-2",
            transaction_date=(day + timedelta(days=2)).isoformat())
        await _txn(
            client, pid, headers, ticker="CASH", transaction_type="withdrawal",
            quantity="100",
            transaction_date=(day + timedelta(days=2)).isoformat())

        response = await client.get(
            f"/internal/portfolios/{pid}/cash-flows", params={
                "start_date": "2026-09-01",
                "end_date": "2026-09-30"
            })
        assert response.status_code == 200
        rows = response.json()
        by_type = [
            (r["transaction_type"], r["amount"], r["quantity"], r["price"])
            for r in rows
        ]
        assert ("deposit", 1000.0, 1000.0, None) in by_type
        assert ("withdrawal", -100.0, 100.0, None) in by_type
        assert ("adjustment", 0.0, 5.0, 120.0) in by_type
        assert ("adjustment", 0.0, -2.0, None) in by_type
        assert all(r["transaction_type"] != "buy" for r in rows)
        assert sum(r["amount"] for r in rows) == 900.0
        assert all("transaction_date" in r and "ticker" in r for r in rows)


class TestSnapshots:

    async def test_snapshot_conversion_and_stale_close_skips(
            self, client, mock_cross_service, db_session, monkeypatch):
        from domains.portfolio.repository import PortfolioSnapshotRepository

        _, headers = _make_token()
        portfolio_id = await create_portfolio(client, headers, "USD Snapshot")

        await PortfolioSnapshotRepository(db_session).upsert(
            portfolio_id=UUID(portfolio_id), snapshot_date=date(2026, 3, 1),
            total_value=Decimal("10000"), cash_balance=Decimal("500"))
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/portfolios/{portfolio_id}/snapshots", headers=headers)
        assert resp.status_code == 200
        rows = resp.json()["data"]
        assert len(rows) == 1
        assert Decimal(rows[0]["total_value"]) == Decimal("10000")
        assert rows[0]["total_value_converted"] is None
        assert rows[0]["fx_rate"] is None
        assert rows[0]["display_currency"] is None

        import clients.ai_client as ai_client
        from domains.portfolio.service import PortfolioService

        _, headers = _make_token()
        stale = await _portfolio_with_positions(
            client, headers, "AAPL", "MSFT")
        cash_only = await _portfolio_with_positions(client, headers)

        async def _closes(ticker, **_kwargs):
            if ticker == "MSFT":
                return {date(2026, 9, 28): Decimal("300")}
            return {SESSION: Decimal("120")}

        monkeypatch.setattr(ai_client, "get_daily_closes", _closes)
        _only_portfolios(monkeypatch, stale, cash_only)
        result = await PortfolioService(db_session).run_market_close_job(
            SESSION)
        assert (result.written, result.skipped) == (1, 1)
        assert await _snapshot(db_session, stale, SESSION) is None
        snap = await _snapshot(db_session, cash_only, SESSION)
        assert snap is not None and snap.total_value == Decimal("10000")

        import api.internal as internal
        import clients.ai_client as ai_client

        _, headers = _make_token()
        pid = await _portfolio_with_positions(client, headers, "AAPL")

        async def _no_closes(ticker, **_kwargs):
            return None

        monkeypatch.setattr(ai_client, "get_daily_closes", _no_closes)
        monkeypatch.setattr(internal, "_market_today", lambda: SESSION)
        _only_portfolios(monkeypatch, pid)
        body = (await client.post("/internal/scheduler/market-close")).json()
        assert body["skipped"] is False
        assert body["date"] == SESSION.isoformat()
        assert body["portfolios_skipped"] == 1
        from job_health import MARKET_CLOSE_SKIPPED

        assert MARKET_CLOSE_SKIPPED._value.get() == 1

    def test_snapshot_script_day_selection_and_checks(self):
        from scripts.recompute_snapshots import days_to_check, session_close

        closes = {date(2026, 9, 25): Decimal("10")}
        assert session_close(closes, date(2026, 9, 28)) is None
        assert session_close(closes, date(2026, 9, 27)) == Decimal("10")
        assert session_close(closes, date(2026, 9, 25)) == Decimal("10")

        stored = [date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 30)]
        assert days_to_check(stored, None, None) == [
            date(2026, 9, 25),
            date(2026, 9, 26),
            date(2026, 9, 28),
            date(2026, 9, 29),
            date(2026, 9, 30)
        ]
        assert days_to_check(stored, date(2026, 9, 29), date(2026, 10, 1)) == [
            date(2026, 9, 29),
            date(2026, 9, 30),
            date(2026, 10, 1)
        ]
        assert days_to_check([], None, None) == []

        from scripts.recompute_snapshots import check_days

        ledger = LEDGER[:2]
        closes = {
            "AAPL": {
                date(2026, 9, 2): Decimal("101"),
                date(2026, 9, 3): Decimal("102")
            }
        }
        stored = {
            date(2026, 9, 2): (Decimal("10010.40"), Decimal("9000")),
            date(2026, 9, 3): (Decimal("10000"), Decimal("9000")),
            date(2026, 9, 4): (Decimal("10000"), Decimal("9000"))
        }
        days = [
            date(2026, 9, 2),
            date(2026, 9, 3),
            date(2026, 9, 4),
            date(2026, 9, 1)
        ]
        checks = {c.day: c for c in check_days(ledger, stored, closes, days)}
        assert checks[date(2026, 9, 2)].action == "ok"
        assert checks[date(2026, 9, 3)].action == "fix"
        assert checks[date(2026, 9, 3)].total == Decimal("10020")
        assert checks[date(2026, 9, 4)].action == "no close"
        assert checks[date(2026, 9, 4)].missing == ["AAPL"]
        assert checks[date(2026, 9, 1)].action == "add"
        assert checks[date(2026, 9, 1)].total == Decimal("10000")

        stored = {date(2026, 9, 1): (Decimal("10000"), Decimal("9999"))}
        (check, ) = check_days(LEDGER[:1], stored, {}, [date(2026, 9, 1)])
        assert check.action == "fix"

    async def test_recompute_preview_then_confirm(
            self, client, db_session, monkeypatch, capsys):
        import clients.ai_client as ai_client
        import scripts.recompute_snapshots as script
        from domains.portfolio.repository import PortfolioSnapshotRepository
        from domains.portfolio.service import PortfolioService

        _, headers = _make_token()
        pid = await _portfolio_with_positions(client, headers, "AAPL")
        service = PortfolioService(db_session)
        holdings = await service.holdings.list_for_portfolio(pid)
        await service._compute_snapshot(pid, SESSION, holdings, {})
        await db_session.flush()

        async def _closes(ticker, **_kwargs):
            return {date(2026, 9, 28): Decimal("110"), SESSION: Decimal("120")}

        monkeypatch.setattr(ai_client, "get_daily_closes", _closes)

        class _NoCommit:

            async def __aenter__(self):
                return db_session

            async def __aexit__(self, *exc):
                return False

        async def _commit():
            await db_session.flush()

        async def _noop():
            return None

        class _Engine:

            async def dispose(self):
                return None

        monkeypatch.setattr(script, "SessionLocal", _NoCommit)
        monkeypatch.setattr(script, "engine", _Engine())
        monkeypatch.setattr(script, "close_redis_connections", _noop)
        monkeypatch.setattr(db_session, "commit", _commit)

        assert await script.main(["--portfolio", str(pid)]) == 0
        out = capsys.readouterr().out
        assert "FIX" in out and "1 row(s) would be written" in out
        snap = await _snapshot(db_session, pid, SESSION)
        assert snap is not None and snap.total_value == Decimal("10000")

        assert await script.main(["--portfolio", str(pid), "--confirm"]) == 0
        db_session.expire_all()
        rows = await PortfolioSnapshotRepository(db_session
                                                 ).list_for_portfolio(pid)
        assert [(r.snapshot_date, r.total_value)
                for r in rows] == [(SESSION, Decimal("10200"))]

        async def _closes_later(ticker, **_kwargs):
            return {SESSION: Decimal("120"), date(2026, 9, 30): Decimal("125")}

        monkeypatch.setattr(ai_client, "get_daily_closes", _closes_later)
        assert (
            await script.main(
                ["--portfolio",
                 str(pid), "--to", "2026-09-30", "--confirm"]) == 0)
        snap = await _snapshot(db_session, pid, date(2026, 9, 30))
        assert snap is not None and snap.total_value == Decimal("10250")


class TestScheduledJobs:

    async def test_jobs_skip_closed_days_and_status_follows_failures(
            self, client, monkeypatch):
        for case_id, path, count_key, method in (
            ("market-close", "/internal/scheduler/market-close",
             "portfolios_processed", "run_market_close_job"),
            ("market-open", "/internal/scheduler/market-open",
             "analyses_requested", "run_market_open_job")):
            import api.internal as internal
            from domains.portfolio.service import (MarketCloseResult,
                                                   MarketOpenResult,
                                                   PortfolioService)

            runs: list[str] = []

            async def _run(self, *args, **kwargs):
                runs.append(method)
                if method == "run_market_close_job":
                    return MarketCloseResult(written=3, skipped=1)
                return MarketOpenResult(requested=3, failed=0)

            monkeypatch.setattr(PortfolioService, method, _run)

            monkeypatch.setattr(
                internal, "_market_today", lambda: date(2026, 11, 26))
            response = await client.post(path)
            assert response.status_code == 200, case_id
            assert response.json() == {
                "skipped": True,
                "reason": "market holiday",
                "date": "2026-11-26",
                count_key: 0
            }, case_id
            monkeypatch.setattr(
                internal, "_market_today", lambda: date(2026, 11, 28))
            reason = (await client.post(path)).json()["reason"]
            assert reason == "weekend", case_id
            assert runs == [], case_id

            monkeypatch.setattr(
                internal, "_market_today", lambda: date(2026, 11, 25))
            response = await client.post(path)
            assert response.status_code == 200, case_id
            expected = {"skipped": False, "date": "2026-11-25", count_key: 3}
            if method == "run_market_close_job":
                expected["portfolios_skipped"] = 1
            else:
                expected["analyses_failed"] = 0
            assert response.json() == expected, case_id
            assert runs == [method], case_id

        for case_id, requested, failed, status in (("all-failed", 2, 2, 500),
                                                   ("some-failed", 2, 1,
                                                    200), ("none-requested", 0,
                                                           0, 200)):

            async def _run(self):
                return MarketOpenResult(requested=requested, failed=failed)

            monkeypatch.setattr(PortfolioService, "run_market_open_job", _run)
            monkeypatch.setattr(
                internal, "_market_today", lambda: date(2026, 11, 25))
            response = await client.post("/internal/scheduler/market-open")
            assert response.status_code == status, case_id
            body = response.json()
            assert body["analyses_requested"] == requested, case_id
            assert body["analyses_failed"] == failed, case_id
            assert ("all_units_failed" in body) == (status == 500), case_id

    async def test_market_open_job_owners_and_failed_requests(
            self, client, db_session, monkeypatch):
        import clients.ai_client as ai_client
        from domains.portfolio.service import PortfolioService

        owner, headers = _make_token()
        pid = await _enable_auto_analysis(client, headers, "nvda")

        calls = []

        async def _req(ticker, portfolio_id=None, *, user_id=None):
            calls.append((ticker, portfolio_id, user_id))
            return None

        rec_calls = []

        async def _rec(ticker, portfolio_id):
            rec_calls.append((ticker, portfolio_id))
            return None

        monkeypatch.setattr(ai_client, "request_analysis", _req)
        monkeypatch.setattr(ai_client, "get_latest_recommendation", _rec)

        await PortfolioService(db_session).run_market_open_job()
        assert ("NVDA", UUID(pid), UUID(owner)) in calls

        await client.get(f"/api/v1/portfolios/{pid}/holdings", headers=headers)
        assert rec_calls == [("NVDA", UUID(pid))]

        import clients.ai_client as ai_client
        import domains.portfolio.service as service_module

        owner, headers = _make_token()
        await _enable_auto_analysis(client, headers, "amd")
        calls: list[tuple] = []

        async def _req(ticker, portfolio_id=None, *, user_id=None):
            calls.append((ticker, portfolio_id, user_id))
            return None

        async def _state(settings, user_id):
            if str(user_id) == owner:
                return UserState(role="user", is_active=False)
            return UserState(role="user", is_active=True)

        monkeypatch.setattr(ai_client, "request_analysis", _req)
        monkeypatch.setattr(service_module, "get_user_state", _state)
        await service_module.PortfolioService(db_session).run_market_open_job()
        assert not any(user_id == UUID(owner) for _, _, user_id in calls)

        import clients.ai_client as ai_client
        import domains.portfolio.service as service_module

        owner, headers = _make_token()
        await _enable_auto_analysis(client, headers, "amd", "msft")

        async def _req(ticker, portfolio_id=None, *, user_id=None):
            return None if ticker.upper() == "AMD" else {"id": "x"}

        async def _state(settings, user_id):
            return UserState(role="user", is_active=str(user_id) == owner)

        monkeypatch.setattr(ai_client, "request_analysis", _req)
        monkeypatch.setattr(service_module, "get_user_state", _state)
        result = await service_module.PortfolioService(db_session
                                                       ).run_market_open_job()
        assert result.requested >= 2
        assert result.failed >= 1
        assert result.failed < result.requested


class TestExports:

    async def _history_portfolio(self, client, db_session, headers):
        from domains.portfolio.repository import PortfolioSnapshotRepository

        pid = await create_portfolio(client, headers)
        snaps = PortfolioSnapshotRepository(db_session)
        await _txn_at(
            client, pid, headers, datetime(2026, 9, 1, 14, tzinfo=UTC),
            ticker="CASH", transaction_type="deposit", quantity="1000")
        await _txn_at(
            client, pid, headers, datetime(2026, 9, 2, 14, tzinfo=UTC),
            ticker="CASH", transaction_type="deposit", quantity="50")
        for day, total, cash in [(1, "1000", "1000"), (2, "1100", "450"),
                                 (3, "990", "450")]:
            await snaps.upsert(
                portfolio_id=UUID(pid), snapshot_date=date(2026, 9, day),
                total_value=Decimal(total), cash_balance=Decimal(cash))
        return pid

    async def test_history_csv_content_and_filters(self, client, db_session):
        _, headers = _make_token()
        pid = await self._history_portfolio(client, db_session, headers)

        response = await client.get(
            f"/api/v1/portfolios/{pid}/history.csv", headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers["content-disposition"] == (
            f'attachment; filename="portfolio-{pid}-history.csv"')
        rows = _csv_rows(response)
        assert rows[0] == [
            "date", "total_value", "cash_balance", "holdings_value",
            "daily_return"
        ]
        assert rows[1] == ["2026-09-01", "1000", "1000", "0", ""]
        assert rows[2][:4] == ["2026-09-02", "1100", "450", "650"]
        assert Decimal(rows[2][4]) == Decimal("0.05")
        assert Decimal(rows[3][4]) == Decimal(990) / Decimal(1100) - 1

        ranged = _csv_rows(
            await client.get(
                f"/api/v1/portfolios/{pid}/history.csv", params={
                    "from": "2026-09-02",
                    "to": "2026-09-02"
                }, headers=headers))
        assert [r[0] for r in ranged[1:]] == ["2026-09-02"]
        assert Decimal(ranged[1][4]) == Decimal("0.05")

    async def test_transactions_csv_content_and_filters(self, client):
        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        await _txn_at(
            client, pid, headers, datetime(2026, 9, 1, 14, tzinfo=UTC),
            ticker="CASH", transaction_type="deposit", quantity="1000")
        await _txn_at(
            client, pid, headers, datetime(2026, 9, 2, 14, tzinfo=UTC),
            ticker="AAPL", transaction_type="buy", quantity="2", price="100.5")
        await _txn_at(
            client, pid, headers, datetime(2026, 9, 3, 14, tzinfo=UTC),
            ticker="AAPL", transaction_type="adjustment", quantity="-1")

        response = await client.get(
            f"/api/v1/portfolios/{pid}/transactions.csv", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert (
            'filename="portfolio-' in response.headers["content-disposition"])
        rows = _csv_rows(response)
        assert rows[0] == [
            "id", "transaction_date", "transaction_type", "ticker", "quantity",
            "price", "dividend_per_share", "split_ratio", "idempotency_key"
        ]
        assert [(r[2], r[3], r[4], r[5])
                for r in rows[1:]] == [("deposit", "CASH", "1000", ""),
                                       ("buy", "AAPL", "2", "100.5"),
                                       ("adjustment", "AAPL", "-1", "")]

        only_buys = _csv_rows(
            await client.get(
                f"/api/v1/portfolios/{pid}/transactions.csv",
                params={"type": "buy"}, headers=headers))
        assert [r[2] for r in only_buys[1:]] == ["buy"]
        ranged = _csv_rows(
            await client.get(
                f"/api/v1/portfolios/{pid}/transactions.csv", params={
                    "from": "2026-09-02",
                    "to": "2026-09-03"
                }, headers=headers))
        assert [r[2] for r in ranged[1:]] == ["buy", "adjustment"]
        from_only = _csv_rows(
            await client.get(
                f"/api/v1/portfolios/{pid}/transactions.csv",
                params={"from": "2026-09-03"}, headers=headers))
        assert [r[2] for r in from_only[1:]] == ["adjustment"]

    async def test_csv_invalid_dates_and_access_rules(
            self, client, monkeypatch):
        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        for path in ("history.csv", "transactions.csv"):
            for case_id, params in (("reversed-range", {"from": "2026-09-05",
                                                        "to": "2026-09-01"}),
                                    ("malformed-from", {"from": "not-a-date"}),
                                    ("impossible-month", {"to": "2026-13-01"
                                                          })):
                response = await client.get(
                    f"/api/v1/portfolios/{pid}/{path}", params=params,
                    headers=headers)
                assert response.status_code == 422, (path, case_id)

        for path in ("history.csv", "transactions.csv"):
            import clients.auth_client as auth_client

            _, owner = _make_token()
            _, stranger = _make_token()
            _, member = _make_token()
            org_id = uuid4()

            async def _orgs(user_id):
                return [org_id]

            async def _roles(user_id):
                return {org_id: "member"}

            monkeypatch.setattr(
                auth_client, "list_user_organization_ids", _orgs)
            monkeypatch.setattr(
                auth_client, "list_user_organization_roles", _roles)
            private = await create_portfolio(client, owner, "private")
            shared = (
                await client.post(
                    "/api/v1/portfolios", json={
                        "name": "shared",
                        "organization_id": str(org_id),
                        "visibility": "organization"
                    }, headers=owner)).json()["data"]["id"]

            assert (
                await client.get(
                    f"/api/v1/portfolios/{private}/{path}",
                    headers=owner)).status_code == 200, path
            monkeypatch.setattr(
                auth_client, "list_user_organization_ids", _no_orgs)
            assert (
                await client.get(
                    f"/api/v1/portfolios/{private}/{path}",
                    headers=stranger)).status_code == 404, path
            assert (
                await client.get(
                    f"/api/v1/portfolios/{shared}/{path}",
                    headers=stranger)).status_code == 404, path
            monkeypatch.setattr(
                auth_client, "list_user_organization_ids", _orgs)
            assert (
                await client.get(
                    f"/api/v1/portfolios/{shared}/{path}",
                    headers=member)).status_code == 200, path
            assert (
                await client.get(
                    f"/api/v1/portfolios/{private}/{path}",
                    headers=member)).status_code == 404, path
            assert (
                await client.get(f"/api/v1/portfolios/{private}/{path}")
            ).status_code == 401, path


class TestWriteSideEffects:

    async def test_failed_delete_publishes_nothing(
            self, client, notified_users, published_events):
        _, owner = _make_token()
        _, stranger = _make_token()
        pid = await create_portfolio(client, owner)
        response = await client.delete(
            f"/api/v1/portfolios/{pid}", headers=stranger)
        assert response.status_code == 404
        assert published_events == []
        assert [n["type"] for n in notified_users] == ["portfolio_created"]

    async def test_notifications_for_every_write(self, client, notified_users):
        user_id, headers = _make_token()
        pid = await create_portfolio(client, headers, "Notify")
        await client.patch(
            f"/api/v1/portfolios/{pid}", json={"name": "Notify 2"},
            headers=headers)
        await _txn_at(
            client, pid, headers, datetime.now(UTC), ticker="CASH",
            transaction_type="deposit", quantity="100")
        holding = (
            await client.post(
                f"/api/v1/portfolios/{pid}/holdings", json={
                    "ticker": "AAPL",
                    "quantity": "2",
                    "average_cost": "10"
                }, headers=headers)).json()["data"]
        await client.patch(
            f"/api/v1/portfolios/{pid}/holdings/{holding['id']}", json={
                "quantity": "3",
                "average_cost": "12"
            }, headers=headers)
        await client.delete(
            f"/api/v1/portfolios/{pid}/holdings/{holding['id']}",
            headers=headers)
        wl = (
            await client.post(
                "/api/v1/watchlists", json={"name": "Tech"},
                headers=headers)).json()["data"]
        await client.patch(
            f"/api/v1/watchlists/{wl['id']}", json={"name": "Tech 2"},
            headers=headers)
        await client.post(
            f"/api/v1/watchlists/{wl['id']}/items", json={"ticker": "msft"},
            headers=headers)
        await client.delete(
            f"/api/v1/watchlists/{wl['id']}/items/msft", headers=headers)
        await client.delete(f"/api/v1/watchlists/{wl['id']}", headers=headers)
        key = (
            await client.post(
                "/api/v1/api-keys", json={"name": "k"},
                headers=headers)).json()["data"]
        await client.patch(
            f"/api/v1/api-keys/{key['id']}", json={"name": "k2"},
            headers=headers)
        await client.delete(f"/api/v1/api-keys/{key['id']}", headers=headers)
        await client.delete(f"/api/v1/portfolios/{pid}", headers=headers)

        assert [n["type"] for n in notified_users] == [
            "portfolio_created", "portfolio_updated", "transaction_created",
            "transaction_created", "transaction_created",
            "transaction_created", "watchlist_created", "watchlist_updated",
            "watchlist_updated", "watchlist_updated", "watchlist_deleted",
            "api_key_created", "api_key_updated", "api_key_revoked",
            "portfolio_deleted"
        ]
        assert all(user_id in n["user_ids"] for n in notified_users)
        assert "adjustment -2, adjustment +3" in notified_users[4]["content"]
        assert (
            "MSFT was added to watchlist 'Tech 2'"
            in notified_users[8]["content"])

    async def test_watchlist_rename_and_delete(self, client):
        _, headers = _make_token()
        _, stranger = _make_token()
        wl = (
            await client.post(
                "/api/v1/watchlists", json={"name": "Old"},
                headers=headers)).json()["data"]
        await client.post(
            f"/api/v1/watchlists/{wl['id']}/items", json={"ticker": "AAPL"},
            headers=headers)
        renamed = await client.patch(
            f"/api/v1/watchlists/{wl['id']}", json={"name": "New"},
            headers=headers)
        assert renamed.status_code == 200
        assert renamed.json()["data"]["name"] == "New"
        assert [i["ticker"]
                for i in renamed.json()["data"]["items"]] == ["AAPL"]
        assert (
            await client.patch(
                f"/api/v1/watchlists/{wl['id']}", json={"name": ""},
                headers=headers)).status_code == 422
        assert (
            await client.patch(
                f"/api/v1/watchlists/{wl['id']}", json={"name": "S"},
                headers=stranger)).status_code == 404
        assert (
            await
            client.delete(f"/api/v1/watchlists/{wl['id']}",
                          headers=stranger)).status_code == 404
        assert (
            await
            client.delete(f"/api/v1/watchlists/{wl['id']}",
                          headers=headers)).status_code == 204
        listed = (await client.get("/api/v1/watchlists",
                                   headers=headers)).json()["data"]
        assert listed == []
        assert (
            await
            client.delete(f"/api/v1/watchlists/{wl['id']}",
                          headers=headers)).status_code == 404


class TestInternalQueries:

    async def test_internal_stats_counts(self, client):
        before = await client.get("/internal/stats")
        assert before.status_code == 200
        counts = before.json()
        assert set(counts) == {
            "portfolios", "transactions", "notes", "comments", "documents",
            "watchlists"
        }
        assert all(isinstance(v, int) for v in counts.values())

        _, headers = _make_token()
        pid = await create_portfolio(client, headers)
        await _txn_at(
            client, pid, headers, datetime.now(UTC), ticker="CASH",
            transaction_type="deposit", quantity="1")
        await client.post(
            "/api/v1/watchlists", json={"name": "S"}, headers=headers)

        after = (await client.get("/internal/stats")).json()
        assert after["portfolios"] == counts["portfolios"] + 1
        assert after["transactions"] == counts["transactions"] + 1
        assert after["watchlists"] == counts["watchlists"] + 1
        assert after["notes"] == counts["notes"]

    async def test_distinct_owner_tickers_and_tracked_tickers_endpoint(
            self, db_session, client, mock_cross_service):
        from domains.portfolio.repository import (HoldingRepository,
                                                  PortfolioRepository,
                                                  WatchlistRepository)

        portfolio_repo = PortfolioRepository(db_session)
        holding_repo = HoldingRepository(db_session)
        owner1, owner2 = uuid4(), uuid4()
        p1 = await portfolio_repo.create(
            owner_id=owner1, name="P1", currency="USD", organization_id=None,
            visibility="private")
        p2 = await portfolio_repo.create(
            owner_id=owner2, name="P2", currency="USD", organization_id=None,
            visibility="private")
        await holding_repo.create(
            portfolio_id=p1.id, ticker="AMD", quantity=Decimal("10"),
            average_cost=Decimal("100"))
        await holding_repo.create(
            portfolio_id=p2.id, ticker="AMD", quantity=Decimal("5"),
            average_cost=Decimal("90"))
        await holding_repo.create(
            portfolio_id=p2.id, ticker="MSFT", quantity=Decimal("3"),
            average_cost=Decimal("400"))
        await db_session.commit()

        pairs = await holding_repo.list_all_owner_tickers()
        mine = sorted(p for p in pairs if p[0] in (owner1, owner2))
        assert mine == sorted([(owner1, "AMD"), (owner2, "AMD"),
                               (owner2, "MSFT")])

        watchlist_repo = WatchlistRepository(db_session)
        w1 = await watchlist_repo.create(owner_id=owner1, name="W1")
        w2 = await watchlist_repo.create(owner_id=owner2, name="W2")
        w3 = await watchlist_repo.create(owner_id=owner2, name="W3")
        await watchlist_repo.add_item(watchlist_id=w1.id, ticker="QCOM")
        await watchlist_repo.add_item(watchlist_id=w2.id, ticker="QCOM")
        await watchlist_repo.add_item(watchlist_id=w2.id, ticker="INTC")
        await watchlist_repo.add_item(watchlist_id=w3.id, ticker="INTC")
        await db_session.commit()

        pairs = await watchlist_repo.list_all_owner_tickers()
        mine = sorted(p for p in pairs if p[0] in (owner1, owner2))
        assert mine == sorted([(owner1, "QCOM"), (owner2, "QCOM"),
                               (owner2, "INTC")])

        from domains.portfolio.repository import (HoldingRepository,
                                                  PortfolioRepository,
                                                  WatchlistRepository)

        portfolio = await PortfolioRepository(db_session).create(
            owner_id=uuid4(), name="P", currency="USD", organization_id=None,
            visibility="private")
        await HoldingRepository(db_session).create(
            portfolio_id=portfolio.id, ticker="AMD", quantity=Decimal("1"),
            average_cost=Decimal("100"))
        watchlist = await WatchlistRepository(db_session).create(
            owner_id=uuid4(), name="W")
        await WatchlistRepository(db_session).add_item(
            watchlist_id=watchlist.id, ticker="QCOM")
        await db_session.commit()

        resp = await client.get("/internal/tracked-tickers")
        assert resp.status_code == 200
        assert {"AMD", "QCOM"} <= set(resp.json())

    async def test_cash_flows_are_filtered_and_signed(
            self, db_session, client, mock_cross_service):
        from domains.portfolio.repository import (PortfolioRepository,
                                                  TransactionRepository)

        portfolio = await PortfolioRepository(db_session).create(
            owner_id=uuid4(), name="Cash Flow Test", currency="USD",
            organization_id=None, visibility="private")
        repo = TransactionRepository(db_session)

        in_window = datetime(2026, 3, 5, tzinfo=UTC)
        before_window = datetime(2026, 1, 1, tzinfo=UTC)
        after_window = datetime(2026, 6, 1, tzinfo=UTC)

        await repo.create(
            portfolio_id=portfolio.id, ticker="CASH",
            transaction_type="deposit", quantity=Decimal("1000"), price=None,
            dividend_per_share=None, transaction_date=in_window)
        await repo.create(
            portfolio_id=portfolio.id, ticker="CASH",
            transaction_type="withdrawal", quantity=Decimal("200"), price=None,
            dividend_per_share=None, transaction_date=in_window)
        await repo.create(
            portfolio_id=portfolio.id, ticker="AAPL", transaction_type="buy",
            quantity=Decimal("10"), price=Decimal("100"),
            dividend_per_share=None, transaction_date=in_window)
        await repo.create(
            portfolio_id=portfolio.id, ticker="AAPL",
            transaction_type="dividend", quantity=Decimal("10"), price=None,
            dividend_per_share=Decimal("0.5"), transaction_date=in_window)
        await repo.create(
            portfolio_id=portfolio.id, ticker="CASH",
            transaction_type="deposit", quantity=Decimal("5000"), price=None,
            dividend_per_share=None, transaction_date=before_window)
        await repo.create(
            portfolio_id=portfolio.id, ticker="CASH",
            transaction_type="deposit", quantity=Decimal("5000"), price=None,
            dividend_per_share=None, transaction_date=after_window)
        await db_session.commit()

        flows = await repo.list_cash_flows_for_portfolio(
            portfolio.id, start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 31))

        assert len(flows) == 2
        assert {f.transaction_type for f in flows} == {"deposit", "withdrawal"}

        from domains.portfolio.repository import (PortfolioRepository,
                                                  TransactionRepository)

        portfolio = await PortfolioRepository(db_session).create(
            owner_id=uuid4(), name="Cash Flow Endpoint Test", currency="USD",
            organization_id=None, visibility="private")
        repo = TransactionRepository(db_session)
        flow_date = datetime(2026, 4, 10, tzinfo=UTC)

        await repo.create(
            portfolio_id=portfolio.id, ticker="CASH",
            transaction_type="deposit", quantity=Decimal("1000"), price=None,
            dividend_per_share=None, transaction_date=flow_date)
        await repo.create(
            portfolio_id=portfolio.id, ticker="CASH",
            transaction_type="withdrawal", quantity=Decimal("300"), price=None,
            dividend_per_share=None, transaction_date=flow_date)
        await db_session.commit()

        resp = await client.get(
            f"/internal/portfolios/{portfolio.id}/cash-flows", params={
                "start_date": "2026-04-01",
                "end_date": "2026-04-30"
            })
        assert resp.status_code == 200
        amounts = sorted(f["amount"] for f in resp.json())
        assert amounts == [-300.0, 1000.0]
