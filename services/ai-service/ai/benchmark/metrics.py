import math

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
