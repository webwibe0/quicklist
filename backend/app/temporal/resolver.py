import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, Field

from app.schemas.commands import EventType, Intent, ParsedCommand
from app.temporal.intervals import scheduled_duration
from app.temporal.recurrence import RecurrenceError, parse_recurrence, resolve_recurrence_start

WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2,
    "четверг": 3, "пятницу": 4, "пятница": 4, "субботу": 5,
    "суббота": 5, "воскресенье": 6,
}
NUMBERS = {
    "один": 1, "одну": 1, "одного": 1, "одна": 1, "час": 1,
    "два": 2, "две": 2, "двух": 2, "три": 3, "трех": 3,
    "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12,
}
NUMBERS.update(dict(zip((
    'ноль', 'тринадцать', 'четырнадцать', 'пятнадцать', 'шестнадцать',
    'семнадцать', 'восемнадцать', 'девятнадцать', 'двадцать', 'тридцать', 'сорок', 'пятьдесят',
), (0, 13, 14, 15, 16, 17, 18, 19, 20, 30, 40, 50))))
for _tens in ('двадцать', 'тридцать', 'сорок', 'пятьдесят'):
    for _ones in ('один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять'):
        NUMBERS[_tens + ' ' + _ones] = NUMBERS[_tens] + NUMBERS[_ones]

SPOKEN_HOUR = '|'.join(re.escape(word) for word, value in sorted(NUMBERS.items(), key=lambda item: -len(item[0]))
                       if value <= 23 and word != 'час')
SPOKEN_MINUTE = '|'.join(re.escape(word) for word in sorted(NUMBERS, key=len, reverse=True) if word != 'час')
WORD_MINUTES_PATTERN = (r'(?:в |на )?(' + SPOKEN_HOUR + r')(?: час(?:а|ов)?)? ('
                        + SPOKEN_MINUTE + r')(?: минут(?:а|ы)?)?')
ELAPSED_PATTERN = r'через (?:полчаса|полтора часа|(?:(?:\d+|[а-я]+(?: [а-я]+)?) )?(?:час(?:а|ов)?|минут[ау]?|минут))'


def elapsed_delta(expression: str) -> timedelta | None:
    text = clean(expression)
    if text in ('через полчаса', 'через полтора часа'):
        return timedelta(minutes=30 if text == 'через полчаса' else 90)
    match = re.fullmatch(r'через (?:(\d+|[а-я]+(?: [а-я]+)?) )?(час(?:а|ов)?|минут[ау]?|минут)', text)
    if not match:
        return None
    amount = number(match[1]) if match[1] else 1
    if not 1 <= amount <= 10000:
        raise TemporalAmbiguity('Укажите интервал от 1 до 10000.')
    return timedelta(hours=amount) if match[2].startswith('час') else timedelta(minutes=amount)


class TemporalAmbiguity(ValueError):
    pass


MONTH_NAMES = (
    ("январь", "января", "январе"), ("февраль", "февраля", "феврале"),
    ("март", "марта", "марте"), ("апрель", "апреля", "апреле"),
    ("май", "мая", "мае"), ("июнь", "июня", "июне"),
    ("июль", "июля", "июле"), ("август", "августа", "августе"),
    ("сентябрь", "сентября", "сентябре"), ("октябрь", "октября", "октябре"),
    ("ноябрь", "ноября", "ноябре"), ("декабрь", "декабря", "декабре"),
)
MONTHS = {name: month for month, names in enumerate(MONTH_NAMES, 1) for name in names}
MONTH_END = re.compile(r"(?:последний день|последнее число|конец) (?:(этого|текущего|прошлого|следующего) )?месяца")


def temporal_direction(text: str) -> Literal["past", "future"] | None:
    text = MONTH_END.sub(" ", clean(text))
    past = re.search(r"\b(?:был[аои]?|состоял[а-я]*|прош[а-я]*|последн[а-я]*|вчера|позавчера)\b", text) or re.search(
        r"\bза (?:\d+|одну|две|два|три|четыре|пять|шесть|семь) (?:день|дня|дней|недел[юьи]|недель|месяц[аев]*)\b", text,
    )
    future = re.search(r"\b(?:будет|будут|будущ[а-я]*|предстоящ[а-я]*|следующ[а-я]*)\b", text)
    return "past" if past and not future else "future" if future and not past else None


ORDINAL_DAYS = {}
for _day, _name in enumerate((
    "первое", "второе", "третье", "четвертое", "пятое", "шестое", "седьмое", "восьмое", "девятое",
    "десятое", "одиннадцатое", "двенадцатое", "тринадцатое", "четырнадцатое", "пятнадцатое",
    "шестнадцатое", "семнадцатое", "восемнадцатое", "девятнадцатое", "двадцатое",
), 1):
    ORDINAL_DAYS[_name] = _day
    ORDINAL_DAYS[_name[:-2] + ("ьего" if _name.endswith("ье") else "ого")] = _day
for _name, _day in list(ORDINAL_DAYS.items()):
    if _day < 10:
        ORDINAL_DAYS["двадцать " + _name] = 20 + _day
    if _day == 1:
        ORDINAL_DAYS["тридцать " + _name] = 31
ORDINAL_DAYS.update(тридцатое=30, тридцатого=30)


def monthless_date(text, today, direction):
    last = MONTH_END.fullmatch(text)
    if last:
        offset = {"этого": 0, "текущего": 0, "прошлого": -1, "следующего": 1}.get(last[1], 0)
        end = today.replace(day=1) + relativedelta(months=offset + 1) - timedelta(days=1)
        if last[1] is None and direction == "past" and end > today:
            end = today.replace(day=1) - timedelta(days=1)
        return end
    qualified = re.fullmatch(r"(.+?) (этого|текущего|прошлого|следующего) месяца", text)
    day_text = qualified[1] if qualified else text
    numeric = re.fullmatch(r"(\d{1,2})(?:-?(?:е|го|ого))?(?: числ(?:о|а))?", day_text)
    day = int(numeric[1]) if numeric else ORDINAL_DAYS.get(day_text)
    if day is None:
        return None
    if not 1 <= day <= 31:
        raise TemporalAmbiguity("Число месяца должно быть от 1 до 31.")
    if qualified:
        offset = {"этого": 0, "текущего": 0, "прошлого": -1, "следующего": 1}[qualified[2]]
        month = today.replace(day=1) + relativedelta(months=offset)
        try:
            return month.replace(day=day)
        except ValueError as exc:
            raise TemporalAmbiguity("В указанном месяце нет такого числа. Уточните дату.") from exc
    step = -1 if direction == "past" else 1
    for offset in range(13):
        month = today.replace(day=1) + relativedelta(months=offset * step)
        try:
            candidate = month.replace(day=day)
        except ValueError:
            continue  # A missing month day is skipped, never clamped to month end.
        if candidate <= today if step == -1 else candidate >= today:
            return candidate
    raise TemporalAmbiguity("Уточните месяц и год.")


def rolling_calendar_days(expression):
    text = clean(expression)
    if re.fullmatch(r"(?:на )?(?:ближайшую )?неделю(?: вперед)?", text):
        return 7
    match = re.fullmatch(r"(?:на )?(?:(?:ближайшие|следующие) )?(\d+|[а-я]+) (?:день|дня|дней)", text)
    return number(match[1]) if match else None


def calendar_parts(text: str) -> tuple[int, int, int | None] | None:
    numeric = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", text)
    named = re.fullmatch(r"(\d{1,2}) ([а-я]+)(?: (\d{4})(?: года)?)?", text)
    match = numeric or named
    if match and (numeric or match[2] in MONTHS):
        return int(match[2]) if numeric else MONTHS[match[2]], int(match[1]), int(match[3]) if match[3] else None
    return None


def calendar_date(parts, today: date, direction="future") -> date:
    month, day, year = parts
    if year is not None:
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise TemporalAmbiguity("Такой даты нет. Укажите корректную дату.") from exc
    candidates = []
    for candidate_year in range(max(1, today.year - 8), min(9999, today.year + 8) + 1):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue
        if direction == "past" and candidate > today or direction == "future" and candidate < today:
            continue
        candidates.append(candidate)
    if not candidates:
        raise TemporalAmbiguity("Такой даты нет. Укажите корректную дату с годом.")
    return min(candidates, key=lambda candidate: abs((candidate - today).days))


def calendar_range(left, right, today: date, direction=None) -> tuple[date, date]:
    lm, ld, ly = left
    rm, rd, ry = right
    wraps = (rm, rd) < (lm, ld)
    if ly is None and ry is None and lm == rm and rd < ld:
        raise TemporalAmbiguity("Конец периода раньше начала. Укажите обе даты с годом.")
    if ly is not None or ry is not None:
        start = calendar_date((lm, ld, ly if ly is not None else ry - int(wraps)), today)
        end = calendar_date((rm, rd, ry if ry is not None else ly + int(wraps)), today)
        if end < start:
            raise TemporalAmbiguity("Конец периода раньше начала. Укажите обе даты.")
        return start, end
    candidates = []
    for year in range(max(1, today.year - 8), min(9999 - int(wraps), today.year + 8) + 1):
        try:
            start, end = date(year, lm, ld), date(year + int(wraps), rm, rd)
        except ValueError:
            continue
        if direction == "past" and end > today or direction == "future" and start < today:
            continue
        candidates.append((start, end))
    if not candidates:
        raise TemporalAmbiguity("В указанном периоде нет корректных дат. Укажите год.")
    return min(candidates, key=lambda span: 0 if span[0] <= today <= span[1] else
               min(abs((span[0] - today).days), abs((span[1] - today).days)))


DAYPARTS = {"утра": "morning", "утром": "morning", "дня": "afternoon", "днем": "afternoon",
            "вечера": "evening", "вечером": "evening", "ночи": "night", "ночью": "night"}
FLEXIBLE_TIMES = {"любое", "любое время", "в любое время", "без разницы", "мне без разницы",
                  "когда угодно", "неважно", "не важно", "время неважно",
                  "весь день", "на весь день", "без указания времени"}
TIME_PATTERN = re.compile(
    r"(\d{1,2}|[а-я]+)(?::(\d{2}))?(?: час(?:а|ов)?)?"
    r"(?: (утра|утром|дня|днем|вечера|вечером|ночи|ночью))?"
)
ORDINAL_HOURS = dict(zip((
    "первого", "второго", "третьего", "четвертого", "пятого", "шестого",
    "седьмого", "восьмого", "девятого", "десятого", "одиннадцатого", "двенадцатого",
), range(1, 13)))
SPOKEN_PART = r"(?:утра|утром|дня|днем|вечера|вечером|ночи|ночью)"
SPOKEN_ZERO_HOUR = r"(?:ноль|один|два|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|двенадцать|двадцать(?: один|два|три)?)"
SPOKEN_CLOCK_PATTERN = (
    r"(?:в |на )?(?:(?:половине|половина|четверть) (?:" + "|".join(ORDINAL_HOURS)
    + r")|" + SPOKEN_ZERO_HOUR + r" ноль[- ]ноль)(?: " + SPOKEN_PART + r")?"
    + r'|' + WORD_MINUTES_PATTERN + r'(?: ' + SPOKEN_PART + r')?'
)


class TimeFragments(BaseModel):
    hour: int | None = Field(default=None, ge=0, le=23)
    daypart: Literal["morning", "afternoon", "evening", "night"] | None = None
    exact_time: time | None = None
    flexible: bool = False

    def merge(self, newer: "TimeFragments") -> "TimeFragments":
        if newer.exact_time is not None or newer.flexible:
            return newer
        if newer.hour is not None:
            return newer.model_copy(update={"daypart": newer.daypart or self.daypart})
        if newer.daypart:
            return newer.model_copy(update={"hour": self.hour})
        return newer

    def combined_expression(self) -> str | None:
        if self.hour is not None and self.daypart:
            suffix = {"morning": "утра", "afternoon": "дня", "evening": "вечера", "night": "ночи"}
            return f"{self.hour} {suffix[self.daypart]}"
        return None


class TemporalResult(BaseModel):
    event_date: date | None = None
    event_time: time | None = None
    event_times: list[time] = Field(default_factory=list)
    time_fragments: TimeFragments = Field(default_factory=TimeFragments)
    end_time: time | None = None
    interval_requested: bool = False
    interval_start_expression: str | None = None
    interval_end_expression: str | None = None
    overnight: bool = False
    time_unspecified: bool = False
    search_direction: Literal["past", "future"] | None = None
    period_start: date | None = None
    period_end: date | None = None
    issues: dict[str, str] = Field(default_factory=dict)


TEMPORAL_WORDS = (
    "сегодня", "завтра", "послезавтра", "вчера", "позавчера",
    "утром", "утра", "вечером", "вечера", "днем", "дня", "ночью", "ночи",
    "полдень", "полночь",
)
STRETCHED_WORDS = [
    (word, re.compile(r"\b" + "".join(letter + "+" for letter in word) + r"\b"))
    for word in TEMPORAL_WORDS
]


def normalize_temporal_words(value: str) -> str:
    """Whole-word whitelist only: stretched known words and the safe truncation 'завтр'."""
    text = value.casefold().replace("ё", "е")
    for word, pattern in STRETCHED_WORDS:
        text = pattern.sub(word, text)
    text = re.sub(r"\bзавтр\b", "завтра", text)
    # Explicit whole-word corrections only; never fuzzy-match arbitrary date-like words.
    text = re.sub(r"\b(?:помлезавтра|послезавтр)\b", "послезавтра", text)
    return text


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_temporal_words(value).strip(" .!?"))


def number(value: str) -> int:
    if value.isdigit():
        return int(value)
    if value in NUMBERS:
        return NUMBERS[value]
    raise TemporalAmbiguity("Укажите число цифрами.")


class TemporalResolver:
    def __init__(self, timezone: str):
        self.zone = ZoneInfo(timezone)

    def local_now(self, now: datetime | None = None) -> datetime:
        if now is None:
            return datetime.now(self.zone)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(self.zone)

    def resolve_date(self, expression: str, now: datetime, *, direction=None) -> tuple[date, time | None]:
        now = self.local_now(now)
        text = re.sub(r"^(?:начиная с|на|в|к|до|с|со|за) ", "", clean(expression))
        # Date extraction keeps the user's grammatical case; resolve known cases,
        # without autocorrecting titles or arbitrary words.
        text = {"понедельника": "понедельник", "понедельнику": "понедельник",
                "вторника": "вторник", "вторнику": "вторник",
                "среды": "среда", "среде": "среда", "четверга": "четверг",
                "четвергу": "четверг", "пятницы": "пятница", "пятнице": "пятница",
                "субботы": "суббота", "субботе": "суббота",
                "воскресенья": "воскресенье", "воскресенью": "воскресенье"}.get(text, text)
        if text == "завтрашнего дня":
            text = "завтра"
        today = now.date()
        monthless = monthless_date(text, today, direction)
        if monthless is not None:
            return monthless, None
        if re.search(r"\bили\b", text):
            raise TemporalAmbiguity("Выберите одну дату.")
        if text in ("сегодня", "завтра", "послезавтра", "вчера", "позавчера"):
            offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2,
                      "вчера": -1, "позавчера": -2}[text]
            return today + timedelta(days=offset), None
        if text == "через неделю":
            return today + timedelta(days=7), None
        qualified = re.fullmatch(r'(сегодня|завтра|послезавтра) (через .+)', text)
        duration = elapsed_delta(qualified[2] if qualified else text)
        if duration is not None:
            target = (now.astimezone(UTC) + duration).astimezone(self.zone).replace(second=0, microsecond=0)
            if qualified and self.resolve_date(qualified[1], now)[0] != target.date():
                raise TemporalAmbiguity('Указанная дата и интервал от текущего момента расходятся. Уточните дату.')
            return target.date(), target.time().replace(tzinfo=None)
        relative = re.fullmatch(r"через (?:(\d+|[а-я]+) )?(час(?:а|ов)?|минут[ау]?|минут|д(?:ень|ня|ней))", text)
        if relative:
            amount = number(relative[1]) if relative[1] else 1
            if not 1 <= amount <= 10000:
                raise TemporalAmbiguity("Укажите интервал от 1 до 10000.")
            unit = relative[2]
            if unit.startswith("д"):
                return today + timedelta(days=amount), None
            # Elapsed hours/minutes are measured on the UTC timeline, including DST.
            delta = timedelta(hours=amount) if unit.startswith("час") else timedelta(minutes=amount)
            target = (now.astimezone(UTC) + delta).astimezone(self.zone).replace(second=0, microsecond=0)
            return target.date(), target.time().replace(tzinfo=None)
        if text in ("конца недели", "конец недели", "до конца недели"):
            return today + timedelta(days=6 - today.weekday()), None
        day = next((name for name in WEEKDAYS if re.search(rf"\b{name}\b", text)), None)
        if day:
            target = WEEKDAYS[day]
            if re.search(r"следующ", text):
                return today + timedelta(days=7 - today.weekday() + target), None
            if re.search(r"(?:эт[уаой]|текущ)", text):
                return today + timedelta(days=target - today.weekday()), None
            if re.sub(rf"\b{day}\b", "", text).strip():
                raise TemporalAmbiguity("Укажите конкретную дату.")
            offset = (target - today.weekday()) % 7
            if offset == 0:
                raise TemporalAmbiguity("Это сегодня или через неделю? Укажите дату или «сегодня».")
            return today + timedelta(days=offset), None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            try:
                return date.fromisoformat(text), None
            except ValueError as exc:
                raise TemporalAmbiguity("Такой даты нет. Укажите корректную дату.") from exc
        parts = calendar_parts(text)
        if parts:
            return calendar_date(parts, today, direction or "future"), None
        raise TemporalAmbiguity("Уточните дату, например «завтра» или «16.10.2026».")

    def resolve_time(self, expression: str) -> time:
        text = clean(expression)
        spoken = re.fullmatch(WORD_MINUTES_PATTERN + r'(?: (' + SPOKEN_PART + r'))?', text)
        if spoken:
            text = f'{number(spoken[1])}:{number(spoken[2]):02}'
            if spoken[3]:
                text += ' ' + spoken[3]
        fraction = re.fullmatch(
            r"(?:в |на )?(половине|половина|четверть) (" + "|".join(ORDINAL_HOURS)
            + r")(?: (" + SPOKEN_PART + r"))?", text,
        )
        if fraction:
            hour = (ORDINAL_HOURS[fraction[2]] - 1) % 12 or 12
            minute = 15 if fraction[1] == "четверть" else 30
            if not fraction[3]:
                raise TemporalAmbiguity("Уточните часть дня: утром или вечером?")
            text = f"{hour}:{minute:02} {fraction[3]}"
        zero = re.fullmatch(r"(?:в |на )?(" + SPOKEN_ZERO_HOUR + r") ноль[- ]ноль(?: (" + SPOKEN_PART + r"))?", text)
        if zero:
            hours = {"ноль": 0, "двадцать": 20, "двадцать один": 21,
                     "двадцать два": 22, "двадцать три": 23}
            hour = hours[zero[1]] if zero[1] in hours else number(zero[1])
            text = str(hour) if 1 <= hour <= 12 and not zero[2] else f"{hour}:00"
            if zero[2]:
                text += " " + zero[2]
        prefix = re.fullmatch(r"(утром|вечером|днем|ночью) (?:в )?(.+)", text)
        if prefix:
            text = prefix[2] + " " + prefix[1]
        if re.search(r"\b(?:до|по)\b|[–—-]", text):
            raise TemporalAmbiguity("Нужен один момент времени, а не диапазон. Укажите, например, 19:00.")
        if re.search(r"\bили\b", text):
            raise TemporalAmbiguity(f"Выберите одно время: {text}.")
        if re.search(r"\b(?:примерно|около)\b|\bчасов в\b", text):
            raise TemporalAmbiguity("Укажите точное время в формате ЧЧ:ММ.")
        text = re.sub(r"^(?:в|на) ", "", text)
        day_parts = {"утром": "утром", "утра": "утром", "вечером": "вечером",
                     "вечера": "вечером", "днем": "днём", "дня": "днём",
                     "ночью": "ночью", "ночи": "ночью"}
        if text in day_parts:
            example = {"утром": "09:00", "вечером": "19:00", "днём": "14:00", "ночью": "01:00"}
            part = day_parts[text]
            raise TemporalAmbiguity(f"Во сколько {part}? Укажите, например, {example[part]}.")
        if text == "полдень":
            return time(12)
        if text == "полночь":
            return time(0)
        match = TIME_PATTERN.fullmatch(text)
        if not match:
            raise TemporalAmbiguity("Укажите точное время в формате ЧЧ:ММ.")
        try:
            hour = number(match[1])
        except TemporalAmbiguity as exc:
            raise TemporalAmbiguity("Укажите точное время в формате ЧЧ:ММ.") from exc
        minute = int(match[2] or 0)
        suffix = match[3]
        if hour > 23 or minute > 59:
            raise TemporalAmbiguity("Некорректное время. Укажите ЧЧ:ММ от 00:00 до 23:59.")
        if suffix:
            if not 1 <= hour <= 12:
                raise TemporalAmbiguity("Укажите время без противоречащего указания части дня.")
            if suffix in ("дня", "днем", "вечера", "вечером"):
                hour = hour % 12 + 12
            elif hour == 12:
                if suffix in ("ночи", "ночью"):
                    hour = 0
                else:
                    raise TemporalAmbiguity("Для 12 часов уточните: 00:00 или 12:00.")
        elif match[2] is None and 1 <= hour <= 12:
            raise TemporalAmbiguity(f"Вы имеете в виду {hour:02}:00 или {hour % 12 + 12:02}:00? Укажите ЧЧ:ММ.")
        return time(hour, minute)

    def time_fragments(self, expression: str) -> TimeFragments:
        text = re.sub(r"^(?:в|на) ", "", clean(expression))
        if clean(expression) in FLEXIBLE_TIMES:
            return TimeFragments(flexible=True)
        if text in DAYPARTS:
            return TimeFragments(daypart=DAYPARTS[text])
        match = TIME_PATTERN.fullmatch(text)
        if match and match[2] is None and match[3] is None:
            hour = number(match[1])
            if 1 <= hour <= 12:
                return TimeFragments(hour=hour)
        clock = self.resolve_time(expression)
        return TimeFragments(
            hour=number(match[1]) if match else clock.hour,
            daypart=DAYPARTS.get(match[3]) if match else None, exact_time=clock,
        )

    def resolve_interval(self, start: str, end: str, *, daypart: str | None = None):
        suffixes = {"morning": "утра", "afternoon": "дня", "evening": "вечера", "night": "ночи"}
        start = re.sub(r"^(?:с|в|на)\s+", "", clean(start))
        end = re.sub(r"^(?:до|по|в)\s+", "", clean(end))
        next_day = end.endswith(" следующего дня")
        if next_day:
            end = end.removesuffix(" следующего дня")
        left, right = TIME_PATTERN.fullmatch(start), TIME_PATTERN.fullmatch(end)
        left_part = DAYPARTS.get(left[3]) if left else None
        right_part = DAYPARTS.get(right[3]) if right else None
        shared = left_part or right_part or daypart

        def endpoint(text, match, part):
            if match and not part and shared and 1 <= number(match[1]) <= 12:
                text += " " + suffixes[shared]
            return self.resolve_time(text)

        result = TemporalResult(interval_requested=True, interval_start_expression=start, interval_end_expression=end)
        for field, expression, match, part in (("time", start, left, left_part), ("end_time", end, right, right_part)):
            try:
                setattr(result, "event_time" if field == "time" else field, endpoint(expression, match, part))
            except TemporalAmbiguity as exc:
                result.issues[field] = str(exc)
        begin, finish = result.event_time, result.end_time
        if begin is not None and finish is not None:
            result.overnight = finish < begin and (next_day or (
                left_part in {"evening", "afternoon"} and right_part in {"night", "morning"}
            ))
            if finish == begin or finish < begin and not result.overnight:
                result.issues["end_time"] = "Окончание должно быть позже начала. Уточните время или переход на следующий день."
        return result

    def resolve_period(self, expression: str, now: datetime, *, direction=None) -> tuple[date, date]:
        now = self.local_now(now)
        today = now.date()
        text = clean(expression)
        if re.search(r"\bили\b", text):
            raise TemporalAmbiguity("Выберите один период.")
        direction = direction or temporal_direction(text)
        single_day = monthless_date(re.sub(r"^(?:на|в|к|до|с|со) ", "", text), today, direction)
        if single_day is not None:
            return single_day, single_day
        if text == "за неделю":
            direction = "past"
        amount = rolling_calendar_days(text)
        if direction != "past" and amount is not None:
            if not 1 <= amount <= 3660:
                raise TemporalAmbiguity("Укажите период от 1 до 3660 дней.")
            return today, today + timedelta(days=amount - 1)
        if re.fullmatch(r"(?:за )?(?:(?:последнюю|прошедшую) )?неделю", text) and direction == "past":
            return today - timedelta(days=7), today
        if re.fullmatch(r"(?:на )?(?:(?:эти|ближайшие|следующие|прошлые) )?выходные", text):
            saturday = today + timedelta(days=5 - today.weekday())
            if "следующие" in text:
                saturday += timedelta(days=7)
            elif "прошлые" in text or direction == "past":
                if saturday + timedelta(days=1) > today:
                    saturday -= timedelta(days=7)
            return saturday, saturday + timedelta(days=1)
        recent = re.fullmatch(
            r"(?:за )?(?:(?:последни[ех]|прошедши[ех]) )?(\d+|[а-я]+) "
            r"(день|дня|дней|неделю|недели|недель|месяц|месяца|месяцев)", text,
        )
        if recent and (recent[1].isdigit() or recent[1] in NUMBERS):
            amount = number(recent[1])
            unit = recent[2]
            if not 1 <= amount <= 3660:
                raise TemporalAmbiguity("Укажите период от 1 до 3660 дней.")
            if unit.startswith("д"):
                start = today - timedelta(days=amount - 1)
            elif unit.startswith("нед") and amount <= 522:
                start = today - timedelta(weeks=amount)
            elif unit.startswith("месяц") and amount <= 120:
                start = today - relativedelta(months=amount)
            else:
                raise TemporalAmbiguity("Слишком большой период. Укажите не больше 10 лет.")
            return start, today
        pair = re.fullmatch(r"(?:на )?(завтра) и (послезавтра)", text)
        if pair:
            return self.resolve_date(pair[1], now)[0], self.resolve_date(pair[2], now)[0]
        pair = re.fullmatch(r"(?:на )?(\d{1,2})\.(\d{1,2}) и (\d{1,2})\.(\d{1,2})", text)
        if pair:
            try:
                start = date(today.year, int(pair[2]), int(pair[1]))
                end = date(today.year, int(pair[4]), int(pair[3]))
            except ValueError as exc:
                raise TemporalAmbiguity("Такой даты нет. Укажите корректные даты периода.") from exc
            if end < start:
                raise TemporalAmbiguity("Конец периода раньше начала. Укажите обе даты.")
            return start, end
        if "недел" in text:
            monday = today - timedelta(days=today.weekday())
            if "конца" in text:
                return today, monday + timedelta(days=6)
            if "следующ" in text:
                return monday + timedelta(days=7), monday + timedelta(days=13)
            if "прошл" in text or "предыдущ" in text:
                return monday - timedelta(days=7), monday - timedelta(days=1)
            if (
                "эт" in text
                or "текущ" in text
                or text in {"неделю", "неделя", "на неделю", "на неделе", "за неделю"}
                or re.fullmatch(r"(?:на\s+)?недел(?:ю|я)\s+впер[её]д", text)
            ):
                return monday, monday + timedelta(days=6)
        if "месяц" in text:
            first = today.replace(day=1)
            if "прошл" in text or "предыдущ" in text:
                return first - relativedelta(months=1), first - timedelta(days=1)
            if "следующ" in text:
                first += relativedelta(months=1)
            elif not ("эт" in text or "текущ" in text):
                raise TemporalAmbiguity("Укажите, какой месяц.")
            return first, first + relativedelta(months=1) - timedelta(days=1)
        named_month = re.fullmatch(r"(?:в |за |на )?([а-я]+)(?: (\d{4})(?: года)?)?", text)
        if named_month and named_month[1] in MONTHS:
            month = MONTHS[named_month[1]]
            year = int(named_month[2] or today.year)
            if not named_month[2]:
                year += -1 if direction == "past" and month > today.month else (
                    1 if direction == "future" and month < today.month else 0)
            try:
                start = date(year, month, 1)
                return start, start + relativedelta(months=1) - timedelta(days=1)
            except (ValueError, OverflowError) as exc:
                raise TemporalAmbiguity("Уточните месяц и год.") from exc
        interval = re.fullmatch(r"(?:с|от) (.+?) (?:по|до) (.+)", text)
        if interval:
            left, right = calendar_parts(interval[1]), calendar_parts(interval[2])
            if left and right:
                return calendar_range(left, right, today, direction)
            start, _ = self.resolve_date(interval[1], now, direction=direction or "current")
            end, _ = self.resolve_date(interval[2], now, direction=direction or "current")
            if end < start:
                raise TemporalAmbiguity("Конец периода раньше начала. Укажите обе даты.")
            return start, end
        single, _ = self.resolve_date(text, now, direction=direction or "current")
        return single, single

    def validate_local_datetime(self, day: date, clock: time):
        naive = datetime.combine(day, clock)
        first = naive.replace(tzinfo=self.zone, fold=0)
        second = naive.replace(tzinfo=self.zone, fold=1)
        valid = [dt for dt in (first, second)
                 if dt.astimezone(UTC).astimezone(self.zone).replace(tzinfo=None) == naive]
        if not valid:
            raise TemporalAmbiguity("Это время отсутствует из-за перевода часов. Укажите другое время.")
        if len(valid) == 2 and first.utcoffset() != second.utcoffset():
            raise TemporalAmbiguity("Это время повторяется при переводе часов. Укажите другое время.")

    def resolve_update(self, parsed, target, now, *, resolved: TemporalResult | None = None):
        shift = parsed.shift_expression
        if not shift and parsed.time_expression and re.search(
                r"вперед|назад|позже|раньше", clean(parsed.time_expression)):
            shift = parsed.time_expression
        if shift:
            result = TemporalResult()
            if (parsed.date_expression or parsed.end_time_expression
                    or (parsed.time_expression and shift != parsed.time_expression)):
                result.issues["time"] = "Укажите либо новое время, либо один сдвиг."
                return result
            match = re.fullmatch(
                r"на (?:(\d+|[а-я]+) )?(час(?:а|ов)?|минут[ау]?|минут|д(?:ень|ня|ней)) "
                r"(вперед|назад|позже|раньше)", clean(shift),
            )
            try:
                if not match:
                    raise TemporalAmbiguity("Уточните сдвиг, например «на час вперед».")
                if target.event_date is None or target.event_time is None:
                    raise TemporalAmbiguity("У события нет точного времени. Укажите дату и время.")
                self.validate_local_datetime(target.event_date, target.event_time)
                amount = number(match[1]) if match[1] else 1
                if not 1 <= amount <= 10000:
                    raise TemporalAmbiguity("Укажите интервал от 1 до 10000.")
                unit = match[2]
                delta = timedelta(**{
                    "hours" if unit.startswith("час") else
                    "days" if unit.startswith("д") else "minutes": amount,
                })
                if match[3] in ("назад", "раньше"):
                    delta = -delta
                anchor = datetime.combine(target.event_date, target.event_time, self.zone)
                shifted = (anchor.astimezone(UTC) + delta).astimezone(self.zone)
                result.event_date = shifted.date()
                result.event_time = shifted.time().replace(tzinfo=None)
                self.validate_local_datetime(result.event_date, result.event_time)
                if target.end_time is not None:
                    duration = scheduled_duration(target.event_date, target.event_time, target.end_time)
                    end = shifted + duration
                    result.end_time = end.time().replace(tzinfo=None)
                    result.overnight = end.date() > shifted.date()
                    self.validate_interval(result)
            except TemporalAmbiguity as exc:
                result.issues["time"] = str(exc)
            return result
        result = self.resolve(parsed, now, time_draft=resolved.time_fragments if resolved else None)
        if resolved and resolved.event_date is not None:
            result.event_date = resolved.event_date
        if not parsed.date_expression and not parsed.time_expression and not parsed.end_time_expression:
            if parsed.new_title:
                result.event_date, result.event_time, result.end_time = target.event_date, target.event_time, target.end_time
            else:
                result.issues["time"] = "На какую дату и время перенести событие?"
            return result
        result.event_date = result.event_date or target.event_date
        result.event_time = result.event_time or target.event_time
        if target.type == "meeting" and target.event_time is None and not parsed.time_expression:
            result.time_unspecified = True
        if target.end_time is not None and result.end_time is None and not result.interval_requested:
            duration = scheduled_duration(target.event_date, target.event_time, target.end_time)
            end = datetime.combine(result.event_date, result.event_time) + duration
            result.end_time = end.time()
            result.overnight = end.date() > result.event_date
        self.validate_interval(result)
        if result.event_date and result.event_time and not result.issues:
            try:
                self.validate_local_datetime(result.event_date, result.event_time)
            except TemporalAmbiguity as exc:
                result.issues["time"] = str(exc)
        return result

    def validate_interval(self, result: TemporalResult):
        if result.end_time is not None:
            result.interval_requested = True
            if result.event_time is not None and (result.end_time == result.event_time
                    or result.end_time < result.event_time and not result.overnight):
                result.issues["end_time"] = "Окончание должно быть позже начала в тот же день. Уточните окончание."
            if result.event_date:
                try:
                    end_date = result.event_date + timedelta(days=int(result.overnight))
                    self.validate_local_datetime(end_date, result.end_time)
                except (TemporalAmbiguity, OverflowError) as exc:
                    result.issues["end_time"] = str(exc)

    def resolve_recurrence_start(self, parsed: ParsedCommand, result: TemporalResult, now: datetime):
        """Also run after a pending patch: a provisional date must follow its new time."""
        if parsed.intent != Intent.CREATE_EVENT or not parsed.recurrence_expression:
            return
        result.issues.pop("recurrence", None)
        # Explicit dates, including ambiguous/invalid ones, must never be replaced.
        if parsed.date_expression or "date" in result.issues:
            return
        try:
            rule = parse_recurrence(parsed.recurrence_expression)
            result.event_date = resolve_recurrence_start(rule, self.local_now(now), result.event_time)
        except RecurrenceError as exc:
            result.event_date = None
            result.issues["recurrence"] = str(exc)

    def resolve(self, parsed: ParsedCommand, now: datetime, *, single_time: bool = False,
                time_draft: TimeFragments | None = None, direction=None) -> TemporalResult:
        result = TemporalResult(search_direction=direction)
        if parsed.date_expression:
            try:
                result.event_date, result.event_time = self.resolve_date(parsed.date_expression, now)
            except TemporalAmbiguity as exc:
                result.issues["date"] = str(exc)
        start_expression = parsed.time_expression
        end_expression = parsed.end_time_expression
        if start_expression and re.fullmatch(r'(?:(?:сегодня|завтра|послезавтра) )?' + ELAPSED_PATTERN, clean(start_expression)):
            try:
                relative_date, relative_time = self.resolve_date(start_expression, now)
                if result.event_date and result.event_date != relative_date:
                    raise TemporalAmbiguity('Указанная дата и интервал от текущего момента расходятся. Уточните дату.')
                result.event_date, result.event_time = relative_date, relative_time
                result.time_fragments.exact_time = relative_time
            except TemporalAmbiguity as exc:
                result.issues['date'] = str(exc)
            start_expression = None
        if (parsed.event_type == EventType.MEETING and start_expression
                and clean(start_expression) in FLEXIBLE_TIMES and not end_expression):
            result.time_unspecified = True
            start_expression = None
        if start_expression:
            try:
                fragments = self.time_fragments(start_expression)
                result.time_fragments = time_draft.merge(fragments) if time_draft else fragments
                start_expression = result.time_fragments.combined_expression() or start_expression
            except TemporalAmbiguity:
                pass  # The existing precise diagnostics below remain authoritative.
        if parsed.time_expressions:
            try:
                if len(parsed.time_expressions) > 12:
                    raise TemporalAmbiguity("Укажите не больше 12 времён напоминания за один запрос.")
                if not (single_time or parsed.event_type == EventType.REMINDER):
                    raise TemporalAmbiguity("Несколько времён поддерживаются только для напоминаний.")
                if start_expression or end_expression:
                    raise TemporalAmbiguity("Уточните отдельные времена напоминаний без интервала.")
                result.event_times = sorted({self.resolve_time(value) for value in parsed.time_expressions})
                result.event_time = result.event_times[0]
            except TemporalAmbiguity as exc:
                result.issues["time"] = str(exc)
                result.event_times = []
                result.event_time = None
        if single_time or parsed.event_type == EventType.REMINDER:
            if end_expression or (start_expression and not re.fullmatch(SPOKEN_CLOCK_PATTERN, clean(start_expression)) and re.search(
                    r"\b(?:с|от|до|по)\b|[–—-]", clean(start_expression))):
                result.issues["time"] = (
                    "Напоминанию нужен один момент времени, а не диапазон. "
                    "Укажите, например, 19:00."
                )
                start_expression = end_expression = None
            elif start_expression and clean(start_expression) in FLEXIBLE_TIMES:
                result.issues["time"] = (
                    "Напоминанию нужно точное время. Укажите время, например, 19:00, "
                    "либо создайте задачу без времени."
                )
                start_expression = None
        if start_expression:
            text = clean(start_expression)
            interval = (None if re.fullmatch(SPOKEN_CLOCK_PATTERN, text) else
                        re.fullmatch(r"(?:с\s+)?(.+?)\s*(?:до|[–—-])\s*(.+)", text))
            if interval:
                start_expression, end_expression = interval.groups()
                if parsed.end_time_expression:
                    try:
                        if self.resolve_time(end_expression) != self.resolve_time(parsed.end_time_expression):
                            result.issues["end_time"] = "Указаны разные времена окончания. Уточните окончание."
                    except TemporalAmbiguity as exc:
                        result.issues["end_time"] = str(exc)
                result.interval_requested = True
            elif text.startswith("с "):
                start_expression = text[2:]
                result.interval_requested = True
            elif text.startswith("до "):
                start_expression, end_expression = None, text[3:]
                result.interval_requested = True
        if start_expression and end_expression:
            result.interval_requested = True
            result.interval_start_expression, result.interval_end_expression = start_expression, end_expression
            interval = self.resolve_interval(start_expression, end_expression,
                                             daypart=time_draft.daypart if time_draft else None)
            result.event_time, result.end_time, result.overnight = interval.event_time, interval.end_time, interval.overnight
            result.issues.update(interval.issues)
            start_expression = end_expression = None
        if start_expression:
            try:
                clock = self.resolve_time(start_expression)
                if result.event_time is not None and clock != result.event_time:
                    raise TemporalAmbiguity("Относительный интервал и указанное время расходятся. Уточните время.")
                result.event_time = clock
                result.time_fragments.exact_time = clock
            except TemporalAmbiguity as exc:
                result.issues["time"] = str(exc)
        if end_expression:
            result.interval_requested = True
            try:
                result.end_time = self.resolve_time(re.sub(r"^до\s+", "", clean(end_expression)))
            except TemporalAmbiguity as exc:
                result.issues["end_time"] = str(exc)
        self.validate_interval(result)
        if parsed.period_expression:
            try:
                scope = {"прошедшие": "past", "прошлые": "past", "старые": "past",
                         "будущие": "future", "предстоящие": "future"}
                if clean(parsed.period_expression) in scope:
                    result.search_direction = scope[clean(parsed.period_expression)]
                else:
                    period_direction = temporal_direction(parsed.period_expression)
                    if rolling_calendar_days(parsed.period_expression) is not None and period_direction != "past":
                        period_direction = None
                    result.search_direction = direction or period_direction
                    result.period_start, result.period_end = self.resolve_period(
                        parsed.period_expression, now, direction=result.search_direction,
                    )
            except TemporalAmbiguity as exc:
                result.issues["period"] = str(exc)
        self.resolve_recurrence_start(parsed, result, now)
        if result.event_date and result.event_time and not result.issues:
            try:
                for clock in result.event_times or [result.event_time]:
                    self.validate_local_datetime(result.event_date, clock)
            except TemporalAmbiguity as exc:
                result.issues["time"] = str(exc)
        return result
