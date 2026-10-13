from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any
import structlog
from ai.llm.base import (AGENT_LLM_CALL_RETRIES, LLMError, LLMMessage,
                         LLMNonEnglishOutputError, LLMRequestError, LLMTier,
                         parse_json_dict)
from ai.llm.router import LLMRouter
from ai.providers.base import ProviderError
from ai.providers.registry import get_market_data_provider
from ai.tools.portfolio_tool import priced_holdings, unpriced_tickers
from clients.core_client import get_portfolio_cash_balance
from config import settings
from pydantic import BaseModel
from shared.enums import RecommendationAction, TierSide
from shared.exceptions import UpstreamServiceError

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are the position-sizing step in AlphaMind's investment research "
    "system. A separate Judge has ALREADY decided the direction (buy or "
    "sell) -- your only job is to propose concrete, real, executable "
    "entry/exit tiers and how long they stay valid. Do not second-guess "
    "the buy/sell direction.\n\n"
    "A recommendation can have more than one tier (e.g. buy some shares "
    "now, buy more at a lower price if it dips) -- propose as many "
    "tiers as genuinely make sense, usually 1-3.\n\n"
    "For a BUY: every tier's (target_price * quantity) summed together "
    "must not exceed the stated BUY budget (available cash minus cash "
    "already committed to the portfolio's other open BUY tiers) -- size "
    "conservatively, this is real capital, not a hypothetical. A proposal "
    "over budget is rejected outright, not scaled down.\n"
    "For a SELL: every tier's quantity summed together must not exceed "
    "the stated current holding quantity -- you cannot sell shares that "
    "aren't held.\n"
    "The current position, total portfolio net value and the ticker's "
    "current weight are given as context for how large a position this "
    "recommendation should build or unwind.\n\n"
    f"Also propose a window (in days, at most "
    f"{settings.TIER_WINDOW_DAYS_MAX}) "
    "this recommendation should "
    "stay valid for before it expires unfulfilled -- judge this per "
    "situation (a fast-moving earnings reaction warrants a shorter "
    "window than a slow-moving valuation thesis), not a fixed default.\n\n"
    'Respond with exactly one JSON object: {"window_days": <int>, '
    '"tiers": [{"target_price": <float>, "quantity": <float>}, ...]}')


class KeepEarlierTiersNote(str):
    __slots__ = ()

    reason_text = "for a budget reason"


class BudgetBlockedNote(KeepEarlierTiersNote):
    __slots__ = ()


class LowConfidenceNote(KeepEarlierTiersNote):
    __slots__ = ()

    reason_text = "because its confidence is below the minimum for tiers"


_BUDGET_OVERSHOOT_TOLERANCE = Decimal("0.005")
_QUANTITY_STEP = Decimal("0.0001")


def _trim_last_tier_to_budget(
        tiers: "list[TierSuggestion]", budget: Decimal, min_notional: Decimal
) -> "tuple[list[TierSuggestion], Decimal] | None":
    total = sum((t.target_price * t.quantity for t in tiers), Decimal(0))
    if budget <= 0 or total - budget > budget * _BUDGET_OVERSHOOT_TOLERANCE:
        return None
    last = tiers[-1]
    room = budget - (total - last.target_price * last.quantity)
    quantity = (room / last.target_price).quantize(
        _QUANTITY_STEP, rounding=ROUND_DOWN)
    if quantity <= 0 or quantity * last.target_price < min_notional:
        return None
    trimmed = TierSuggestion(target_price=last.target_price, quantity=quantity)
    return [*tiers[:-1], trimmed], last.quantity


class TierSuggestion(BaseModel):
    target_price: Decimal
    quantity: Decimal


class PositionSizingResult(BaseModel):
    window_days: int
    tiers: list[TierSuggestion]


class _PositionContext(BaseModel):
    held_quantity: Decimal
    position_value: Decimal
    total_net_value: Decimal | None
    current_weight_pct: Decimal | None
    unpriced: list[str]

    def lines(self, ticker: str) -> list[str]:
        lines = [
            f"Current position in {ticker}: {self.held_quantity:f} shares, "
            f"market value {self.position_value:.2f}"
        ]
        if (self.total_net_value is not None
                and self.current_weight_pct is not None):
            lines.append(
                f"Total portfolio net value (holdings at live prices + cash): "
                f"{self.total_net_value:.2f}")
            lines.append(
                f"Current weight of {ticker} in the portfolio: "
                f"{self.current_weight_pct:.1f}%")
        else:
            missing = ", ".join(self.unpriced) or "cash balance"
            lines.append(
                "Total portfolio net value and current weight: unavailable "
                f"this run (no live value for {missing})")
        return lines


def _holding_totals(priced: list[dict[str, Any]],
                    ticker: str) -> tuple[Decimal, Decimal, Decimal]:
    held_qty = Decimal(0)
    held_value = Decimal(0)
    total = Decimal(0)
    for holding in priced:
        value = holding.get("market_value")
        is_ticker = str(holding.get("ticker", "")).upper() == ticker.upper()
        if is_ticker:
            held_qty += Decimal(str(holding.get("quantity", 0)))
        if value is not None:
            total += Decimal(str(value))
            if is_ticker:
                held_value += Decimal(str(value))
    return held_qty, held_value, total


async def _position_context(
        portfolio_id: str, ticker: str, price: Decimal,
        cash: Decimal | None) -> _PositionContext:
    priced, _ = await priced_holdings(portfolio_id, get_market_data_provider())
    held, _, total = _holding_totals(priced, ticker)
    unpriced = unpriced_tickers(priced)
    position_value = held * price
    total_net: Decimal | None = None
    weight: Decimal | None = None
    if not unpriced and cash is not None:
        total_net = total + cash
        if total_net > 0:
            weight = position_value / total_net * 100
    return _PositionContext(
        held_quantity=held, position_value=position_value,
        total_net_value=total_net, current_weight_pct=weight,
        unpriced=unpriced)


def position_weight_room(
        position: _PositionContext | None,
        limit_pct: Decimal) -> Decimal | None:
    if position is None or position.total_net_value is None:
        return None
    return position.total_net_value * limit_pct / 100 - position.position_value


async def current_position_weight_pct(
        portfolio_id: str, ticker: str) -> float | None:
    try:
        cash = Decimal(str(await get_portfolio_cash_balance(portfolio_id)))
        priced, _ = await priced_holdings(
            portfolio_id, get_market_data_provider())
    except (UpstreamServiceError, ProviderError):
        logger.info("position_sizing.weight_lookup_failed", ticker=ticker)
        return None
    if unpriced_tickers(priced):
        return None
    _, held_value, holdings_total = _holding_totals(priced, ticker)
    total = cash + holdings_total
    if total <= 0:
        return None
    return float(held_value / total * 100)


def min_tier_notional(total_net_value: Decimal | None) -> Decimal:
    floor = Decimal(str(settings.TIER_MIN_NOTIONAL_USD))
    if total_net_value is not None and total_net_value > 0:
        fraction = Decimal(str(settings.TIER_MIN_NOTIONAL_PORTFOLIO_FRACTION))
        share = total_net_value * fraction
        return max(floor, share)
    return floor


async def generate_position_sizing(
    *, ticker: str, action: RecommendationAction, confidence: float,
    reasoning: str, portfolio_id: str,
    committed_buy_cash: Decimal = Decimal(0),
    llm_router: LLMRouter | None = None
) -> tuple[PositionSizingResult | None, str | None]:
    if action == RecommendationAction.HOLD:
        return None, None

    min_confidence = settings.MIN_BUY_TIER_CONFIDENCE
    if action == RecommendationAction.BUY and confidence < min_confidence:
        logger.info(
            "position_sizing.buy_confidence_below_minimum", ticker=ticker,
            confidence=confidence, minimum=min_confidence)
        return None, LowConfidenceNote(
            "No entry tiers were generated for this recommendation -- its "
            f"confidence {confidence:.2f} is below the minimum of "
            f"{min_confidence:.2f} for BUY tiers, so no cash is reserved "
            "for it.")

    market_provider = get_market_data_provider()
    try:
        quote = await market_provider.get_current_price(ticker)
    except ProviderError as exc:
        logger.info(
            "position_sizing.price_lookup_failed", ticker=ticker,
            error=str(exc))
        return None, (
            "No entry/exit tiers were generated for this recommendation -- "
            f"current price data for {ticker} was still unavailable after "
            f"every retry ({exc}).")
    price = Decimal(str(quote.price))

    cash: Decimal | None = None
    try:
        cash = Decimal(str(await get_portfolio_cash_balance(portfolio_id)))
    except UpstreamServiceError as exc:
        logger.info(
            "position_sizing.cash_balance_lookup_failed",
            portfolio_id=portfolio_id, error=str(exc))
        if action == RecommendationAction.BUY:
            return None, (
                "No entry tiers were generated for this recommendation -- "
                f"the portfolio's cash balance could not be looked up ({exc})."
            )

    try:
        position: _PositionContext | None = await _position_context(
            portfolio_id, ticker, price, cash)
    except UpstreamServiceError as exc:
        logger.info(
            "position_sizing.holdings_lookup_failed",
            portfolio_id=portfolio_id, error=str(exc))
        if action == RecommendationAction.SELL:
            return None, (
                "No exit tiers were generated for this recommendation -- "
                f"the portfolio's current holdings could not be looked up "
                f"({exc}).")
        position = None

    budget = Decimal(0)
    capped_by_weight = False
    min_notional = min_tier_notional(
        position.total_net_value if position is not None else None)
    if action == RecommendationAction.BUY:
        assert cash is not None
        budget = cash - committed_buy_cash
        if 0 < budget < min_notional:
            logger.info(
                "position_sizing.budget_below_min_tier", ticker=ticker,
                budget=str(budget), min_notional=str(min_notional))
            return None, BudgetBlockedNote(
                "No entry tiers were generated for this recommendation -- the "
                f"remaining BUY budget {budget:.2f} (available cash "
                f"{cash:.2f} "
                f"minus {committed_buy_cash:.2f} committed to this "
                f"portfolio's "
                f"other open BUY tiers) is below the minimum tier size of "
                f"{min_notional:.2f}.")
        if budget <= 0:
            logger.info(
                "position_sizing.no_buy_budget", ticker=ticker, cash=str(cash),
                committed=str(committed_buy_cash))
            return None, BudgetBlockedNote(
                "No entry tiers were generated for this recommendation -- "
                f"available cash {cash:.2f} minus {committed_buy_cash:.2f} "
                "already committed to this portfolio's other open BUY tiers "
                f"leaves no budget ({budget:.2f}).")
        limit_pct = Decimal(str(settings.MAX_POSITION_WEIGHT_PCT))
        room = position_weight_room(position, limit_pct)
        if room is not None and room < min_notional:
            assert (
                position is not None
                and position.current_weight_pct is not None)
            logger.info(
                "position_sizing.position_weight_limit", ticker=ticker,
                weight_pct=str(round(position.current_weight_pct,
                                     1)), room=str(room))
            return None, (
                "No entry tiers were generated for this recommendation -- "
                f"{ticker} is already {position.current_weight_pct:.1f}% of "
                f"the "
                f"portfolio's net value; the {limit_pct:.0f}% single-position "
                f"limit leaves room for {max(room, Decimal(0)):.2f} more, "
                f"below "
                f"the minimum tier size of {min_notional:.2f}.")
        capped_by_weight = room is not None and room < budget
        if capped_by_weight:
            assert room is not None
            budget = room
        constraint_lines = [
            f"Available cash: {cash:.2f}",
            f"Cash already committed to this portfolio's other open BUY "
            f"tiers: "
            f"{committed_buy_cash:.2f}",
            f"Budget for this recommendation's BUY tiers (hard limit on the "
            f"summed target_price * quantity): {budget:.2f}" + (
                f" (capped so {ticker} stays at or below the {limit_pct:.0f}% "
                "single-position limit of the portfolio's net value)"
                if capped_by_weight else ""),
            f"Minimum size of each BUY tier (target_price * quantity; "
            f"fractional shares are allowed): {min_notional:.2f}"
        ]
    else:
        assert position is not None
        if position.held_quantity <= 0:
            logger.info(
                "position_sizing.sell_with_no_holding", ticker=ticker,
                portfolio_id=portfolio_id)
            return None, (
                "No exit tiers were generated for this recommendation -- "
                f"bearish view; no position held in {ticker} (SELL tiers are "
                "only generated for a held position; short selling is never "
                "proposed).")
        constraint_lines = [
            f"Current holding quantity (hard limit on the summed SELL "
            f"quantity): {position.held_quantity:f}"
        ]
        if cash is not None:
            constraint_lines.append(f"Available cash: {cash:.2f}")

    context_lines = (
        position.lines(ticker) if position is not None else [
            "Current position context: unavailable this run (holdings lookup "
            "failed)"
        ])
    user_content = "\n".join([
        f"Ticker: {ticker}", f"Direction (already decided): {action.value}",
        f"Judge's confidence: {confidence:.2f}",
        f"Judge's reasoning: {reasoning}", f"Current real price: {price:.2f}",
        *context_lines, *constraint_lines
    ])

    router = llm_router or LLMRouter()
    messages = [
        LLMMessage(role="system", content=_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_content)
    ]
    fallback_note: str | None = None
    try:
        response = await router.complete(
            LLMTier.HIGHEST_ACCURACY, messages, retries=AGENT_LLM_CALL_RETRIES)
    except (LLMRequestError, LLMNonEnglishOutputError) as exc:
        logger.info(
            "position_sizing.llm_call_failed_retrying_lite", ticker=ticker,
            error=str(exc))
        try:
            response = await router.complete(
                LLMTier.CHEAP, messages, retries=AGENT_LLM_CALL_RETRIES)
        except LLMError as lite_exc:
            logger.info(
                "position_sizing.lite_retry_failed", ticker=ticker,
                error=str(lite_exc))
            return None, (
                "No entry/exit tiers were generated for this recommendation "
                "-- "
                f"the position-sizing model call failed ({exc}), and the "
                f"retry on the fallback model failed too ({lite_exc}).")
        logger.info(
            "position_sizing.lite_retry_succeeded", ticker=ticker,
            model=response.model)
        fallback_note = (
            "Entry/exit tiers were computed by the fallback model "
            f"({response.model}) after the primary position-sizing model "
            f"call failed ({exc}).")
    except LLMError as exc:
        logger.info(
            "position_sizing.llm_call_failed", ticker=ticker, error=str(exc))
        return None, (
            "No entry/exit tiers were generated for this recommendation -- "
            f"the position-sizing model call failed ({exc}).")

    result, note = _validated_sizing(
        response.content, action=action, ticker=ticker, budget=budget,
        cash=cash, committed_buy_cash=committed_buy_cash,
        capped_by_weight=capped_by_weight, min_notional=min_notional,
        position=position)
    if fallback_note is not None:
        combined = f"{note} {fallback_note}" if note else fallback_note
        note = (
            BudgetBlockedNote(combined)
            if isinstance(note, BudgetBlockedNote) else combined)
    return result, note


def _validated_sizing(
    raw: str, *, action: RecommendationAction, ticker: str, budget: Decimal,
    cash: Decimal | None, committed_buy_cash: Decimal, capped_by_weight: bool,
    min_notional: Decimal, position: _PositionContext | None
) -> tuple[PositionSizingResult | None, str | None]:
    parsed = parse_json_dict(
        raw, log_event="position_sizing.unparseable_response")
    if parsed is None:
        return None, (
            "No entry/exit tiers were generated for this recommendation -- "
            "the position-sizing model's response could not be parsed.")

    window_days_raw = parsed.get("window_days")
    tiers_raw = parsed.get("tiers")
    if not isinstance(window_days_raw, int) or not isinstance(tiers_raw, list):
        logger.warning("position_sizing.malformed_response", parsed=parsed)
        return None, (
            "No entry/exit tiers were generated for this recommendation -- "
            "the position-sizing model's response was missing required "
            "fields.")

    tiers: list[TierSuggestion] = []
    for tier in tiers_raw:
        if not isinstance(tier, dict):
            continue
        try:
            suggestion = TierSuggestion(
                target_price=Decimal(str(tier["target_price"])),
                quantity=Decimal(str(tier["quantity"])))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if not (suggestion.target_price > 0 and suggestion.quantity > 0):
            continue
        tiers.append(suggestion)

    if not tiers:
        logger.warning("position_sizing.no_valid_tiers_parsed", parsed=parsed)
        return None, (
            "No entry/exit tiers were generated for this recommendation -- "
            "the position-sizing model's response contained no tier that "
            "could be parsed into a valid price/quantity.")

    trim_note: str | None = None
    if action == RecommendationAction.BUY:
        small = [
            t for t in tiers if t.target_price * t.quantity < min_notional
        ]
        if small:
            logger.info(
                "position_sizing.small_tiers_dropped", ticker=ticker,
                dropped=len(small), min_notional=str(min_notional))
            tiers = [t for t in tiers if t not in small]
            if not tiers:
                return None, (
                    "No entry tiers were generated for this recommendation -- "
                    "every proposed tier was below the minimum tier size of "
                    f"{min_notional:.2f}.")
        total_cost = sum((t.target_price * t.quantity for t in tiers),
                         Decimal(0))
        trim = (
            _trim_last_tier_to_budget(tiers, budget, min_notional)
            if total_cost > budget else None)
        if trim is not None:
            tiers, old_quantity = trim
            new_total = sum((t.target_price * t.quantity for t in tiers),
                            Decimal(0))
            logger.info(
                "position_sizing.buy_trimmed_to_budget", ticker=ticker,
                total_cost=str(total_cost), budget=str(budget),
                old_quantity=str(old_quantity),
                new_quantity=str(tiers[-1].quantity))
            trim_note = (
                f"The proposed tiers cost {total_cost:.2f}, "
                f"{total_cost - budget:.2f} "
                f"over the budget of {budget:.2f} (within the "
                f"{_BUDGET_OVERSHOOT_TOLERANCE * 100:.1f}% tolerance); the "
                f"last "
                f"tier's quantity was reduced from {old_quantity:f} to "
                f"{tiers[-1].quantity:f} (total {new_total:.2f}).")
        elif total_cost > budget:
            logger.warning(
                "position_sizing.buy_over_budget", ticker=ticker,
                total_cost=str(total_cost), budget=str(budget))
            over_note = (
                "No entry tiers were generated for this recommendation -- the "
                f"proposed tiers would cost {total_cost:.2f} in total, above "
                f"the "
                f"budget of {budget:.2f} (available cash {cash:.2f} minus "
                f"{committed_buy_cash:.2f} committed to this portfolio's "
                f"other "
                "open BUY tiers" + (
                    f", capped by the {settings.MAX_POSITION_WEIGHT_PCT:.0f}% "
                    "single-position limit" if capped_by_weight else "") +
                "). Tiers are scaled down only when the excess is at most "
                f"{_BUDGET_OVERSHOOT_TOLERANCE * 100:.1f}% of the budget.")
            return None, (
                over_note
                if capped_by_weight else BudgetBlockedNote(over_note))
    else:
        assert position is not None
        total_qty = sum((t.quantity for t in tiers), Decimal(0))
        if total_qty > position.held_quantity:
            logger.warning(
                "position_sizing.sell_exceeds_holding", ticker=ticker,
                total_quantity=str(total_qty),
                held=str(position.held_quantity))
            return None, (
                "No exit tiers were generated for this recommendation -- the "
                f"proposed tiers would sell {total_qty:f} shares in total, "
                f"more "
                f"than the {position.held_quantity:f} held (short selling is "
                "never proposed).")

    sizing_note = tier_sizing_note(
        action=action, ticker=ticker, tiers=tiers, position=position,
        budget=budget)
    trim_note = f"{trim_note} {sizing_note}" if trim_note else sizing_note

    window_days = max(1, min(window_days_raw, settings.TIER_WINDOW_DAYS_MAX))
    if window_days != window_days_raw:
        logger.info(
            "position_sizing.window_days_clamped", proposed=window_days_raw,
            used=window_days)
    return (
        PositionSizingResult(window_days=window_days, tiers=tiers), trim_note)


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    return part / whole * 100


def tier_sizing_note(
        *, action: RecommendationAction, ticker: str,
        tiers: list[TierSuggestion], position: _PositionContext | None,
        budget: Decimal) -> str:
    notionals = [t.target_price * t.quantity for t in tiers]
    total = sum(notionals, Decimal(0))
    net = position.total_net_value if position is not None else None
    has_net = net is not None and net > 0
    side = "BUY" if action == RecommendationAction.BUY else "SELL"
    parts: list[str] = []
    for index, (tier, notional) in enumerate(zip(tiers, notionals,
                                                 strict=True), 1):
        share = (
            f", {_pct(notional, net):.1f}%"
            if has_net and net is not None else "")
        parts.append(
            f"tier {index}: {tier.quantity:f} x {tier.target_price:.2f} = "
            f"{notional:.2f}{share}")
    if has_net and net is not None:
        total_text = (
            f"{total:.2f} = {_pct(total, net):.1f}% of the portfolio's net "
            f"value "
            f"of {net:.2f} (holdings at live prices + cash)")
    else:
        missing = ", ".join(position.unpriced) if position is not None else ""
        total_text = (
            f"{total:.2f} (share of the portfolio's net value unavailable "
            f"this "
            f"run{f': no live value for {missing}' if missing else ''})")
    note = (
        f"Tier sizing (computed in code): {len(tiers)} {side} tier(s) "
        f"totalling "
        f"{total_text}; {'; '.join(parts)}.")
    if action == RecommendationAction.BUY:
        if budget > 0:
            note += (
                f" They use {_pct(total, budget):.0f}% of this "
                f"recommendation's "
                f"BUY budget of {budget:.2f}.")
        if has_net and net is not None and position is not None:
            after = _pct(position.position_value + total, net)
            current = position.current_weight_pct or Decimal(0)
            note += (
                f" If every tier fills, {ticker} would be about {after:.1f}% "
                f"of "
                f"net value (now {current:.1f}%).")
    elif position is not None and position.held_quantity > 0:
        sold = sum((t.quantity for t in tiers), Decimal(0))
        note += (
            f" They sell {sold:f} of the {position.held_quantity:f} shares "
            f"held "
            f"({_pct(sold, position.held_quantity):.0f}%).")
    logger.info(
        "position_sizing.tier_sizing_note_built", ticker=ticker, side=side,
        tiers=len(tiers), total_notional=str(round(total, 2)),
        pct_of_net_value=(
            str(round(_pct(total, net), 1)) if has_net and net else None))
    return note


def tier_side_for_action(action: RecommendationAction) -> TierSide:
    if action == RecommendationAction.SELL:
        return TierSide.SELL
    return TierSide.BUY
