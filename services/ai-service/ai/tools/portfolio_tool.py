import asyncio
import math
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID
import structlog
from ai.providers.base import MarketDataProvider, ProviderError
from ai.providers.registry import get_market_data_provider
from ai.tools.base import Tool
from clients.core_client import (get_portfolio_cash_balance,
                                 get_portfolio_holdings)
from config import settings
from database import SessionLocal
from domains.analysis.repository import RecommendationTierRepository
from shared.exceptions import UpstreamServiceError

logger = structlog.get_logger(__name__)

_PortfolioLookupErrors = (UpstreamServiceError, ProviderError)


async def priced_holdings(
        portfolio_id: str, market_provider: MarketDataProvider
) -> tuple[list[dict[str, Any]], float]:
    holdings = await get_portfolio_holdings(portfolio_id)

    async def _price_one(holding: dict[str, Any]) -> dict[str, Any]:
        try:
            quote = await market_provider.get_current_price(holding["ticker"])
            price = float(quote.price)
        except ProviderError:
            price = None
        market_value = (
            price * float(holding["quantity"]) if price is not None else None)
        return {**holding, "price": price, "market_value": market_value}

    priced = list(await asyncio.gather(*[_price_one(h) for h in holdings]))
    total = sum(
        h["market_value"] for h in priced if h["market_value"] is not None)
    missing = unpriced_tickers(priced)
    if missing:
        logger.warning(
            "portfolio_tool.live_price_unavailable", portfolio_id=portfolio_id,
            tickers=missing, priced_count=len(priced) - len(missing))
    return priced, total


async def committed_buy_cash_elsewhere(
        portfolio_id: str, ticker: str) -> Decimal | None:
    try:
        portfolio_uuid = UUID(portfolio_id)
        async with SessionLocal() as session:
            return await RecommendationTierRepository(
                session).committed_buy_cash(
                    portfolio_id=portfolio_uuid, exclude_ticker=ticker)
    except Exception as exc:
        logger.info(
            "portfolio_tool.committed_buy_cash_unavailable",
            portfolio_id=portfolio_id, error=str(exc))
        return None


def unpriced_tickers(priced: list[dict[str, Any]]) -> list[str]:
    return [str(h["ticker"]) for h in priced if h["price"] is None]


class GetPortfolioCompositionTool(Tool):
    name = "get_portfolio_composition"
    description = (
        "Get current portfolio holdings with live market values, "
        "concentration weights, and available cash. action_input: {} "
        "(no arguments needed)")
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False
    }

    def __init__(
            self, portfolio_id: str | None,
            market_provider: MarketDataProvider | None = None) -> None:
        self.portfolio_id = portfolio_id
        self.market_provider = market_provider or get_market_data_provider()

    async def run(self) -> str:  # type: ignore[override]
        if not self.portfolio_id:
            return (
                "No portfolio is attached to this analysis -- "
                "cannot inspect composition.")
        try:
            priced, total = await priced_holdings(
                self.portfolio_id, self.market_provider)
        except _PortfolioLookupErrors as exc:
            return f"Portfolio composition lookup failed: {exc}"

        try:
            cash_balance: float | None = await get_portfolio_cash_balance(
                self.portfolio_id)
        except UpstreamServiceError:
            cash_balance = None
        cash_line = (
            f"Available cash: {cash_balance:.2f}"
            if cash_balance is not None else "Available cash: unavailable")

        if not priced:
            return f"Portfolio has no holdings. {cash_line}."

        missing = unpriced_tickers(priced)
        if missing:
            total_str = (
                f"total market value PARTIAL ~{total:.2f} -- live price "
                f"unavailable for {', '.join(missing)}, so the total and "
                "every weight below cannot be computed")
        else:
            total_str = f"total market value ~{total:.2f}"
        lines = [
            f"Portfolio composition ({len(priced)} holdings, "
            f"{total_str}). {cash_line}."
        ]
        if cash_balance is not None:
            real_total_net_value = total + cash_balance
            denom_label = ""
        else:
            real_total_net_value = total
            denom_label = (
                " [cash unknown -- weight excludes it, likely overstated]")
        cap = settings.MAX_POSITION_WEIGHT_PCT
        above_cap: list[str] = []
        for h in sorted(priced, key=lambda x: x["market_value"] or 0,
                        reverse=True):
            weights = "weight=n/a"
            if h["market_value"] and real_total_net_value and not missing:
                net_pct = h["market_value"] / real_total_net_value * 100
                weights = (
                    f"weight_of_net_value={net_pct:.1f}%{denom_label} "
                    f"weight_of_holdings_value="
                    f"{h['market_value'] / total * 100:.1f}%")
                if cash_balance is not None and net_pct > cap:
                    above_cap.append(f"{h['ticker']} ({net_pct:.1f}%)")
            price_str = (
                f"{h['price']:.2f}"
                if h["price"] is not None else "unavailable")
            value_str = (
                f"{h['market_value']:.2f}"
                if h["market_value"] is not None else "unavailable")
            lines.append(
                f"- {h['ticker']}: qty={h['quantity']} "
                f"avg_cost={h['average_cost']} "
                f"price={price_str} value={value_str} {weights}")
        if cash_balance is not None and not missing:
            lines.append(
                f"Single-holding cap: {cap:g}% of net value (holdings + "
                "cash). Holdings above the cap: " +
                (", ".join(above_cap) or "none") + ".")
        return "\n".join(lines)


SCENARIO_NET_VALUE_FRACTIONS = (0.05, 0.10)


class RecomputeScenarioTool(Tool):
    name = "recompute_scenario"
    description = (
        "Recompute this ticker's portfolio concentration if a hypothetical "
        'additional position were added. action_input: {"ticker": "AAPL", '
        '"additional_shares": 10} -- call this more than once with different '
        "additional_shares values before concluding whether the action "
        "meaningfully changes the portfolio's risk profile.")
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string"
            },
            "additional_shares": {
                "type": "number"
            }
        },
        "required": ["ticker", "additional_shares"],
        "additionalProperties": False
    }

    def __init__(
            self, portfolio_id: str | None,
            market_provider: MarketDataProvider | None = None) -> None:
        self.portfolio_id = portfolio_id
        self.market_provider = market_provider or get_market_data_provider()
        self._cached_priced_holdings: (
            tuple[list[dict[str, Any]], float] | None) = None
        self._cash_balance_fetched: bool = False
        self._cached_cash_balance: float | None = None
        self._committed_buy_cash: dict[str, Decimal | None] = {}

    async def _get_committed_buy_cash(self, ticker: str) -> Decimal | None:
        key = ticker.upper()
        if key not in self._committed_buy_cash:
            assert self.portfolio_id is not None
            self._committed_buy_cash[key] = await committed_buy_cash_elsewhere(
                self.portfolio_id, key)
        return self._committed_buy_cash[key]

    async def _get_priced_holdings(self) -> tuple[list[dict[str, Any]], float]:
        if self._cached_priced_holdings is not None:
            return self._cached_priced_holdings
        assert self.portfolio_id is not None
        snapshot = await priced_holdings(
            self.portfolio_id, self.market_provider)
        if not unpriced_tickers(snapshot[0]):
            self._cached_priced_holdings = snapshot
        return snapshot

    async def _get_cash_balance(self) -> float | None:
        assert self.portfolio_id is not None
        try:
            if not self._cash_balance_fetched:
                self._cached_cash_balance = await get_portfolio_cash_balance(
                    self.portfolio_id)
                self._cash_balance_fetched = True
        except UpstreamServiceError:
            self._cached_cash_balance = None
            self._cash_balance_fetched = True
        return self._cached_cash_balance

    async def suggested_share_counts(self,
                                     ticker: str) -> tuple[int, int] | None:
        if not self.portfolio_id:
            return None
        try:
            priced, total = await self._get_priced_holdings()
            quote = await self.market_provider.get_current_price(ticker)
        except _PortfolioLookupErrors:
            return None
        if unpriced_tickers(priced):
            return None
        cash_balance = await self._get_cash_balance()
        price = float(quote.price)
        if cash_balance is None or price <= 0:
            return None
        net_value = total + cash_balance
        if net_value <= 0:
            return None
        small_fraction, large_fraction = SCENARIO_NET_VALUE_FRACTIONS
        small = max(1, math.floor(net_value * small_fraction / price))
        large = max(small + 1, math.floor(net_value * large_fraction / price))
        return small, large

    async def run(  # type: ignore[override]
        self, ticker: str, additional_shares: float
    ) -> str:
        if not self.portfolio_id:
            return (
                "No portfolio is attached to this analysis -- cannot run a "
                "scenario.")
        try:
            additional_shares = float(additional_shares)
        except (TypeError, ValueError):
            additional_shares = math.nan
        if not math.isfinite(additional_shares):
            return (
                "Scenario recompute failed: additional_shares must be a "
                "finite "
                "number (positive to buy, negative to sell).")
        try:
            priced, total = await self._get_priced_holdings()
            quote = await self.market_provider.get_current_price(ticker)
        except _PortfolioLookupErrors as exc:
            return f"Scenario recompute failed: {exc}"

        await self._get_cash_balance()
        missing = unpriced_tickers(priced)
        if missing:
            return (
                "Scenario recompute failed: live price unavailable for "
                f"{', '.join(missing)}; total net value cannot be computed.")
        cash_balance = self._cached_cash_balance
        if cash_balance is None:
            return (
                "Scenario recompute failed: real cash balance is "
                "currently unavailable, and this scenario's own "
                "concentration math genuinely needs it -- proceeding "
                "without it would silently understate the real total "
                "portfolio value and overstate the real resulting "
                "concentration.")
        real_total_net_value = total + cash_balance

        existing = next(
            (h for h in priced if h["ticker"].upper() == ticker.upper()), None)
        existing_value = (existing["market_value"] or 0.0) if existing else 0.0
        held_quantity = float(existing["quantity"]) if existing else 0.0
        if additional_shares < -held_quantity:
            return (
                f"Scenario recompute failed: "
                f"additional_shares={additional_shares:g} "
                f"would sell more {ticker} than the {held_quantity:g} shares "
                f"held. "
                f"Use a value of at least {-held_quantity:g}.")

        added_value = additional_shares * float(quote.price)
        new_position_value = existing_value + added_value

        old_weight = ((existing_value / real_total_net_value *
                       100) if real_total_net_value else 0.0)
        new_weight = ((new_position_value / real_total_net_value *
                       100) if real_total_net_value else 0.0)

        shortfall = max(0.0, added_value - cash_balance)
        result = (
            f"Scenario: {ticker} {additional_shares:+g} shares at "
            f"{quote.price:.2f}. "
            f"Position concentration: {old_weight:.1f}% -> {new_weight:.1f}% "
            f"of portfolio (total net value, holdings + cash, "
            f"~{real_total_net_value:.2f}).\n"
            f"cost={added_value:.2f}, available_cash={cash_balance:.2f}, "
            f"shortfall={shortfall:.2f}")
        if additional_shares <= 0:
            return result
        committed = await self._get_committed_buy_cash(ticker)
        if committed is None:
            return result + (
                "\ncommitted_to_other_open_buy_tiers=unavailable "
                "(the remaining BUY budget is unknown this run)")
        buy_budget = max(0.0, cash_balance - float(committed))
        budget_shortfall = max(0.0, added_value - buy_budget)
        return result + (
            f"\nCash split: {float(committed):.2f} is already committed "
            "(reserved by other tickers' open BUY tiers); "
            f"{buy_budget:.2f} is the remaining, uncommitted BUY budget for "
            "this ticker."
            f"\ncommitted_to_other_open_buy_tiers={float(committed):.2f}, "
            f"buy_budget={buy_budget:.2f}, "
            f"budget_shortfall={budget_shortfall:.2f}")
