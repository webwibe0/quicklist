import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Literal

RecurrenceFrequency = Literal["daily", "weekly", "monthly"]
DEFAULT_RECURRENCE_DAYS = 90
MAX_RECURRENCE_OCCURRENCES = 200
MAX_RECURRENCE_SPAN_DAYS = 3660


class RecurrenceError(ValueError):
    """Safe domain error; a dialogue clarification, never an unexpected server failure."""


class UnsupportedRecurrenceError(RecurrenceError):
    pass


class InvalidRecurrenceError(RecurrenceError):
    pass


class RecurrenceLimitError(RecurrenceError):
    pass


@dataclass(frozen=True)
class RecurrenceRule:
    frequency: RecurrenceFrequency
    weekdays: tuple[int, ...] = ()
    month_day: int | None = None
    count: int | None = None
    until: date | None = None
    interval: int = 1


WEEKDAY_PHRASES = {
    "каждый понедельник": 0,
    "каждый вторник": 1,
    "каждую среду": 2,
    "каждый четверг": 3,
    "каждую пятницу": 4,
    "каждую субботу": 5,
    "каждое воскресенье": 6,
}

WEEKDAY_NAMES = (
    ('понедельник','понедельникам'), ('вторник','вторникам'), ('среду','средам'),
    ('четверг','четвергам'), ('пятницу','пятницам'), ('субботу','субботам'), ('воскресенье','воскресеньям'),
)
WEEKDAY_ALIASES = {name: day for day,names in enumerate(WEEKDAY_NAMES) for name in names}


def recurrence_fragments(expression: str) -> tuple[str, list[str]]:
    """Split only grammar-defined suffixes; guard can verify each literal fragment."""
    text = ' '.join(expression.casefold().replace('ё','е').split())
    fragments = []
    while match := re.search(r'\s+(?:(?:всего\s+)?\d+\s+раз(?:а)?|до\s+\d{1,2}\.\d{1,2}\.\d{4})$', text):
        fragments.insert(0, match[0].strip())
        text = text[:match.start()]
    return text, fragments


def parse_weekdays(text: str) -> tuple[int, ...]:
    parts = re.split(r'\s*(?:,|\bи\b)\s*', text)
    if not parts or any(part not in WEEKDAY_ALIASES for part in parts):
        raise UnsupportedRecurrenceError('Уточните дни недели, например «по понедельникам и пятницам».')
    return tuple(sorted({WEEKDAY_ALIASES[part] for part in parts}))


def parse_recurrence(expression: str) -> RecurrenceRule:
    text, fragments = recurrence_fragments(expression)

    count = None
    until = None

    count_parts = [part for part in fragments if not part.startswith('до ')]
    until_parts = [part for part in fragments if part.startswith('до ')]
    if len(count_parts) > 1 or len(until_parts) > 1:
        raise InvalidRecurrenceError('Укажите одно количество и одну дату окончания.')
    count_match = re.fullmatch(r"(?:всего\s+)?(\d+)\s+раз(?:а)?", count_parts[0]) if count_parts else None
    if count_match:
        if len(count_match[1]) > 6:
            raise RecurrenceLimitError("Слишком много повторений: максимум 200 событий за запрос.")
        count = int(count_match.group(1))
        if count < 1:
            raise InvalidRecurrenceError("Количество повторений должно быть положительным.")
        if count > MAX_RECURRENCE_OCCURRENCES:
            raise RecurrenceLimitError("Слишком много повторений: максимум 200 событий за запрос.")

    until_match = re.fullmatch(r"до\s+(\d{1,2}\.\d{1,2}\.\d{4})", until_parts[0]) if until_parts else None
    if until_match:
        day, month, year = map(int, until_match.group(1).split("."))
        try:
            until = date(year, month, day)
        except ValueError as exc:
            raise InvalidRecurrenceError("Некорректная дата окончания повторений.") from exc

    if text == "каждый день":
        return RecurrenceRule(
            frequency="daily",
            count=count,
            until=until,
        )

    if text == "по будням":
        return RecurrenceRule(
            frequency="weekly",
            weekdays=(0, 1, 2, 3, 4),
            count=count,
            until=until,
        )

    if text == "каждую неделю":
        return RecurrenceRule(
            frequency="weekly",
            count=count,
            until=until,
        )

    if text in WEEKDAY_PHRASES:
        return RecurrenceRule(
            frequency="weekly",
            weekdays=(WEEKDAY_PHRASES[text],),
            count=count,
            until=until,
        )

    repeated_week = re.fullmatch(r'(?:раз в|каждые) (\d+|[а-я]+) недел(?:ю|и|ь)(?: по (.+))?', text)
    if repeated_week:
        numbers = {'одну':1,'один':1,'две':2,'два':2,'три':3,'четыре':4,'пять':5,'шесть':6,'семь':7,'восемь':8,'девять':9,'десять':10}
        interval = int(repeated_week[1]) if repeated_week[1].isdigit() else numbers.get(repeated_week[1],0)
        if not 1 <= interval <= 52:
            raise RecurrenceLimitError('Интервал повторений должен быть от 1 до 52 недель.')
        return RecurrenceRule(frequency='weekly', interval=interval,
            weekdays=parse_weekdays(repeated_week[2]) if repeated_week[2] else (), count=count, until=until)
    weekday_list = re.fullmatch(r'(?:каждый|каждую|каждое|по) (.+)', text)
    if weekday_list and any(part in text for names in WEEKDAY_NAMES for part in names):
        return RecurrenceRule(frequency='weekly', weekdays=parse_weekdays(weekday_list[1]), count=count, until=until)

    monthly = re.fullmatch(
        r"(?:каждый месяц|каждого месяца)\s+(\d{1,2})\s+числа",
        text,
    )
    if monthly:
        month_day = int(monthly.group(1))
        if not 1 <= month_day <= 31:
            raise InvalidRecurrenceError("Число месяца должно быть от 1 до 31.")

        return RecurrenceRule(
            frequency="monthly",
            month_day=month_day,
            count=count,
            until=until,
        )

    raise UnsupportedRecurrenceError(
        "Не удалось понять правило повторения. Укажите, например, «каждый день», "
        "«по будням» или «каждый понедельник»."
    )
def generate_recurrence_dates(
    start: date,
    rule: RecurrenceRule,
    *,
    horizon_days: int | None = None,
) -> list[date]:
    if horizon_days is not None and horizon_days < 0:
        raise InvalidRecurrenceError("Горизонт повторений не может быть отрицательным.")
    if rule.count is not None and not 1 <= rule.count <= MAX_RECURRENCE_OCCURRENCES:
        raise RecurrenceLimitError("Количество повторений должно быть от 1 до 200.")
    if rule.until and rule.until < start:
        raise InvalidRecurrenceError("Окончание повторений раньше первой даты. Уточните дату.")
    if rule.until and (rule.until - start).days > MAX_RECURRENCE_SPAN_DAYS:
        raise RecurrenceLimitError("Период повторений слишком большой: максимум 10 лет.")
    if horizon_days is not None and horizon_days > MAX_RECURRENCE_SPAN_DAYS:
        raise RecurrenceLimitError("Период повторений слишком большой: максимум 10 лет.")
    if rule.count is not None:
        # Even month-day 31 has a matching date at least every two months.
        # COUNT is bounded by rows, independently of the unbounded/UNTIL horizon.
        spacing = {"daily": 1, "weekly": 7 * rule.interval, "monthly": 62}[rule.frequency]
        span = rule.count * spacing
        if rule.frequency == "weekly" and rule.interval > 1:
            span = min(span, MAX_RECURRENCE_SPAN_DAYS)
    elif rule.until:
        span = (rule.until - start).days
    else:
        span = DEFAULT_RECURRENCE_DAYS - 1 if horizon_days is None else horizon_days
    end = min(date.max, start + timedelta(days=min(span, (date.max - start).days)))
    if rule.until:
        end = min(end, rule.until)

    dates: list[date] = []
    current = start

    while current <= end:
        matches = False

        if rule.frequency == "daily":
            matches = True

        elif rule.frequency == "weekly":
            if rule.weekdays:
                week = (current - (start - timedelta(days=start.weekday()))).days // 7
                matches = current.weekday() in rule.weekdays and week % rule.interval == 0
            else:
                matches = (current - start).days % (7 * rule.interval) == 0

        elif rule.frequency == "monthly":
            matches = current.day == rule.month_day

        if matches:
            dates.append(current)
            if len(dates) > MAX_RECURRENCE_OCCURRENCES:
                raise RecurrenceLimitError("Слишком много повторений: максимум 200 событий за запрос.")

            if rule.count is not None and len(dates) >= rule.count:
                break

        if current == end:
            break
        current += timedelta(days=1)

    if not dates:
        raise InvalidRecurrenceError("В указанном периоде нет повторений. Уточните даты или правило.")
    if rule.count and len(dates) < rule.count and rule.until is None:
        raise RecurrenceLimitError("Не удалось получить все повторения в допустимом диапазоне дат.")
    return dates


def resolve_recurrence_start(
    rule: RecurrenceRule, now: datetime, event_time: time | None, *, explicit_date: date | None = None,
) -> date:
    """Find the first future date with the same calendar matcher used for expansion.

    `now` is the request clock in the application timezone. An implicit timed start
    must be strictly later than now; explicit starts retain the usual past guard.
    Generic weekly recurrence anchors on the nearest eligible day, then every 7 days.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if explicit_date is not None:
        return explicit_date
    start = now.date()
    if event_time is not None and datetime.combine(start, event_time, now.tzinfo) <= now:
        if start == date.max:
            raise RecurrenceLimitError("Нет будущей даты в допустимом диапазоне.")
        start += timedelta(days=1)
    # Only find a date here: no occurrence commands, series UUID, or persistence.
    # UNTIL and calendar matching remain authoritative in the shared generator.
    return generate_recurrence_dates(start, replace(rule, count=1, interval=1))[0]
