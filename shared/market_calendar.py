from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


def market_today(now: datetime | None = None) -> date:
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(MARKET_TZ).date()


EXTRA_CLOSED_DATES: frozenset[date] = frozenset(
    {date(2018, 12, 5), date(2025, 1, 9)})


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month // 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> frozenset[date]:
    days: set[date] = set()
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        days.add(new_year + timedelta(days=1))
    elif new_year.weekday() != 5:
        days.add(new_year)
    days.add(_nth_weekday(year, 1, 0, 3))
    days.add(_nth_weekday(year, 2, 0, 3))
    days.add(_easter_sunday(year) - timedelta(days=2))
    days.add(_last_weekday(year, 5, 0))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    days.add(_observed(date(year, 7, 4)))
    days.add(_nth_weekday(year, 9, 0, 1))
    days.add(_nth_weekday(year, 11, 3, 4))
    days.add(_observed(date(year, 12, 25)))
    days.update(d for d in EXTRA_CLOSED_DATES if d.year == year)
    return frozenset(d for d in days if d.year == year)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def previous_trading_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate
