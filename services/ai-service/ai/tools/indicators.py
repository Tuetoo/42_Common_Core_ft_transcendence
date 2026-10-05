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
