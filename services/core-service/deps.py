from datetime import UTC, date, datetime, time
from config import settings
from shared.exceptions import ValidationAppError
from shared.security import make_get_current_user

get_current_user = make_get_current_user(settings)


def check_date_range(
        start: date | None, end: date | None,
        message: str = "'from' must not be after 'to'.") -> None:
    if start is not None and end is not None and start > end:
        raise ValidationAppError(message)


def day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)
