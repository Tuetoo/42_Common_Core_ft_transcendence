import math

PRICE_VS_LEVEL_BAND_PCT = 0.001


def price_position_vs_level(
        price: float, level: float,
        band_pct: float = PRICE_VS_LEVEL_BAND_PCT) -> str:
    band = max(0.01, abs(price) * band_pct)
    diff = price - level
    if diff > band:
        return "above"
    if diff < -band:
        return "below"
    return "at"


def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def ema_series(closes: list[float], period: int) -> list[float]:
    if len(closes) < period:
        return []
    multiplier = 2 / (period + 1)
    values = [sum(closes[:period]) / period]
    for price in closes[period:]:
        values.append((price - values[-1]) * multiplier + values[-1])
    return values


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


RSI_OVERSOLD_MAX = 30.0
RSI_OVERBOUGHT_MIN = 70.0
RSI_MIDLINE = 50.0


def rsi_band(value: float) -> str:
    shown = round(value, 1)
    if shown <= RSI_OVERSOLD_MAX:
        return "oversold"
    if shown >= RSI_OVERBOUGHT_MIN:
        return "overbought"
    return "neutral-weak" if shown < RSI_MIDLINE else "neutral-firm"


_RSI_BAND_WORDS = {
    "oversold": "oversold (at or below 30)",
    "overbought": "overbought (at or above 70)",
    "neutral-weak": "neutral (lower half, 30-50)",
    "neutral-firm": "neutral (upper half, 50-70)"
}


def rsi_band_words(value: float) -> str:
    return _RSI_BAND_WORDS[rsi_band(value)]


def macd(
        closes: list[float], *, fast: int = 12, slow: int = 26,
        signal: int = 9) -> tuple[float, float, float] | None:
    if len(closes) < slow + signal:
        return None

    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    offset = slow - fast
    macd_line = [
        ema_fast[offset + i] - ema_slow[i] for i in range(len(ema_slow))
    ]

    signal_line_series = ema_series(macd_line, signal)
    if not signal_line_series:
        return None

    macd_now = macd_line[-1]
    signal_now = signal_line_series[-1]
    return macd_now, signal_now, macd_now - signal_now


def bollinger_bands(
        closes: list[float], *, period: int = 20,
        num_std: float = 2.0) -> tuple[float, float, float] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle)**2 for x in window) / period
    std = variance**0.5
    return middle + num_std * std, middle, middle - num_std * std


def atr(
        highs: list[float], lows: list[float], closes: list[float], *,
        period: int = 14) -> float | None:
    if not (len(highs) == len(lows) == len(closes)) or len(highs) < period + 1:
        return None

    true_ranges = [
        max(
            highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])) for i in range(1, len(highs))
    ]

    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def annualized_volatility(
        closes: list[float], *,
        trading_periods_per_year: int = 252) -> float | None:
    if len(closes) < 3:
        return None
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1]
               for i in range(1, len(closes)) if closes[i - 1]]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean)**2 for r in returns) / (len(returns) - 1)
    return float((variance**0.5) * (trading_periods_per_year**0.5))


# Backtest and performance metrics (simple returns, ratios, drawdown).

_TRADING_DAYS_PER_YEAR = 252


def simple_returns(prices: list[float]) -> list[float]:
    if len(prices) < 2:
        return []
    return [(prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices)) if prices[i - 1] != 0]


def paired_simple_returns(
        first: list[float],
        second: list[float]) -> tuple[list[float], list[float]]:
    pairs = [((first[i] - first[i - 1]) / first[i - 1],
              (second[i] - second[i - 1]) / second[i - 1])
             for i in range(1, min(len(first), len(second)))
             if first[i - 1] != 0 and second[i - 1] != 0]
    return [a for a, _ in pairs], [b for _, b in pairs]


def total_return(prices: list[float]) -> float | None:
    if len(prices) < 2 or prices[0] == 0:
        return None
    return (prices[-1] - prices[0]) / prices[0]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((v - mean)**2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def sharpe_ratio(
        returns: list[float], *, risk_free_rate: float = 0.0,
        periods_per_year: int = _TRADING_DAYS_PER_YEAR) -> float | None:
    if len(returns) < 2:
        return None
    period_rf = risk_free_rate / periods_per_year
    excess = [r - period_rf for r in returns]
    std = _stdev(excess)
    if std == 0:
        return None
    return (_mean(excess) / std) * math.sqrt(periods_per_year)


def sortino_ratio(
        returns: list[float], *, risk_free_rate: float = 0.0,
        periods_per_year: int = _TRADING_DAYS_PER_YEAR) -> float | None:
    if len(returns) < 2:
        return None
    period_rf = risk_free_rate / periods_per_year
    excess = [r - period_rf for r in returns]
    downside = [min(e, 0.0) for e in excess]
    downside_std = math.sqrt(sum(d**2 for d in downside) / len(downside))
    if downside_std == 0:
        return None
    return (_mean(excess) / downside_std) * math.sqrt(periods_per_year)


def max_drawdown(prices: list[float]) -> float | None:
    if len(prices) < 2:
        return None
    peak = prices[0]
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak > 0:
            drawdown = (peak - price) / peak
            worst = max(worst, drawdown)
    return worst


def win_rate(trade_returns: list[float]) -> float | None:
    if not trade_returns:
        return None
    wins = sum(1 for r in trade_returns if r > 0)
    return wins / len(trade_returns)


def alpha_beta(
        strategy_returns: list[float],
        benchmark_returns: list[float]) -> tuple[float | None, float | None]:
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return None, None
    s = strategy_returns[:n]
    b = benchmark_returns[:n]
    mean_s, mean_b = _mean(s), _mean(b)
    covariance = sum((s[i] - mean_s) * (b[i] - mean_b) for i in range(n)) / n
    variance_b = sum((x - mean_b)**2 for x in b) / n
    if variance_b == 0:
        return None, None
    beta = covariance / variance_b
    daily_alpha = mean_s - beta * mean_b
    annualized_alpha = daily_alpha * _TRADING_DAYS_PER_YEAR
    return annualized_alpha, beta


def information_ratio(
        strategy_returns: list[float],
        benchmark_returns: list[float]) -> float | None:
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return None
    active_returns = [
        strategy_returns[i] - benchmark_returns[i] for i in range(n)
    ]
    tracking_err = _stdev(active_returns)
    if tracking_err == 0:
        return None
    return (_mean(active_returns) /
            tracking_err) * math.sqrt(_TRADING_DAYS_PER_YEAR)


def tracking_error(
        strategy_returns: list[float],
        benchmark_returns: list[float]) -> float | None:
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return None
    active_returns = [
        strategy_returns[i] - benchmark_returns[i] for i in range(n)
    ]
    return _stdev(active_returns) * math.sqrt(_TRADING_DAYS_PER_YEAR)
