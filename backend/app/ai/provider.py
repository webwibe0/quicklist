import logging
import re
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError

from app.schemas.commands import EventType, Intent, ParsedCommand
from app.services.recurrence_scope import explicit_recurrence_scope
from app.temporal.recurrence import RecurrenceError, parse_recurrence, recurrence_fragments
from app.temporal.resolver import (
    ELAPSED_PATTERN,
    SPOKEN_CLOCK_PATTERN,
    TIME_PATTERN,
    TemporalAmbiguity,
    TemporalResolver,
    elapsed_delta,
    normalize_temporal_words,
)

logger = logging.getLogger(__name__)


def ordinal_index(text: str) -> int | None:
    forms = ("перв", "втор", "трет", "четверт", "пят", "шест", "седьм", "восьм", "девят", "десят")
    text = normalized(text)
    for index, stem in enumerate(forms, 1):
        if re.search(rf"\b{stem}(?:ую|ый|ой|ая|ое|ого|ей|ий|ья|ью|ье)\b", text):
            return index
    numeric = re.search(r"\b(\d+)-?(?:ую|ю|ой|ый|ая)\b", text)
    if not numeric:
        numeric = re.fullmatch(r"(?:номер )?(\d+)", text)
    return int(numeric[1]) if numeric else None


def normalized(text: str) -> str:
    return " ".join(normalize_temporal_words(text).split()).strip(" .!?,")


def temporal_tokens(text: str) -> list:
    """Canonicalize explicit tokens, never infer an omitted year or a relative date.

    Regex only locates tokens; the existing resolver validates and computes values.
    Unrecognized/ambiguous tokens retain their spelling for fail-closed matching.
    Typed tuples cannot be forged by literal marker strings in user input.
    """
    explicit = re.compile(
        r"(?<!\w)(?:"
        r"(?P<month_day>(?:(?:на|в|к) )?\d{1,2}(?:-(?:е|го|ого)| числ(?:о|а)))|"
        r"(?P<date>(?:(?:на|в|к) )?(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|"
        r"\d{1,2} [а-я]+ \d{4}(?: года)?))|"
        r"(?P<partial_date>(?:(?:на|в|к) )?(?:\d{1,2}\.\d{1,2}|\d{1,2} "
        r"(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря)))|"
        r'(?P<elapsed>' + ELAPSED_PATTERN + r')|'
        r"(?P<time>" + SPOKEN_CLOCK_PATTERN + r"|(?:в |на )?(?:\d{1,2}:\d{2}|полдень|полночь|"
        r"(?:\d{1,2}|[а-я]+)(?: час(?:а|ов)?)? "
        r"(?:утра|утром|дня|днем|вечера|вечером|ночи|ночью))|(?:в |на )\d{1,2})"
        r")(?!\w|[.:]\d)",
    )
    resolver = TemporalResolver("UTC")
    # Fixed leap-year anchor; inferred years are discarded for yearless date tokens.
    anchor = datetime(2000, 1, 1, tzinfo=UTC)
    text = normalized(text)
    tokens = []
    end = 0
    for match in explicit.finditer(text):
        tokens.extend(re.findall(r"\w+|[^\w\s]", text[end:match.start()]))
        try:
            if match['elapsed']:
                value = ('elapsed', elapsed_delta(match['elapsed']).total_seconds())
            elif match["month_day"]:
                value = ("month_day", int(re.search(r"\d+", match["month_day"])[0]))
            elif match["date"] or match["partial_date"]:
                day = resolver.resolve_date(match["date"] or match["partial_date"], anchor)[0]
                value = ("date", day if match["date"] else (day.month, day.day))
            else:
                value = ("time", resolver.resolve_time(match["time"]))
            tokens.append(value)
        except TemporalAmbiguity:
            tokens.extend(re.findall(r"\w+|[^\w\s]", match[0]))
        end = match.end()
    tokens.extend(re.findall(r"\w+|[^\w\s]", text[end:]))
    # The resolver defines both 'с/от ... по/до ...' as inclusive date ranges.
    for index in range(len(tokens) - 3):
        if (tokens[index] in ("с", "от")
                and isinstance(tokens[index + 1], tuple) and tokens[index + 1][0] == "date"
                and tokens[index + 2] in ("по", "до")
                and isinstance(tokens[index + 3], tuple) and tokens[index + 3][0] == "date"):
            tokens[index], tokens[index + 2] = "с", "по"
    return tokens


def normalize_command(command: ParsedCommand) -> ParsedCommand:
    """Canonical temporal fields without computing dates or mutating provider output."""
    if (command.intent == Intent.CREATE_EVENT and command.event_type in (None, "REMINDER")
            and command.time_expression and command.time_expressions is None
            and not re.search(r"\bмежду\b", normalized(command.time_expression))):
        parts = re.split(r"\s+и\s+", re.sub(r"^и\s+", "", normalized(command.time_expression)))
        if len(parts) > 1:
            command = command.model_copy(update={"time_expressions": parts, "time_expression": None})
    if (command.intent in (Intent.CREATE_EVENT, Intent.UPDATE_EVENT) and command.time_expression
            and not re.fullmatch(SPOKEN_CLOCK_PATTERN, normalized(command.time_expression))):
        text = normalized(command.time_expression)
        between = re.fullmatch(r"(?:в любое время )?между (.+?) и (.+)", text)
        interval = between or re.fullmatch(r"(?:с\s+)?(.+?)\s*(?:до|[–—-])\s*(.+)", text)
        if interval and not command.end_time_expression:
            command = command.model_copy(update={
                "time_expression": interval[1], "end_time_expression": interval[2],
            })
        elif text.startswith("до ") and not command.end_time_expression:
            command = command.model_copy(update={
                "time_expression": None, "end_time_expression": text[3:],
            })
    if (command.intent in (Intent.LIST_EVENTS, Intent.SEARCH_EVENTS, Intent.DELETE_EVENT, Intent.COMPLETE_EVENT)
            and command.date_expression is not None and command.period_expression is None):
        return command.model_copy(update={
            "period_expression": command.date_expression, "date_expression": None,
        })
    if (command.intent == Intent.CREATE_EVENT and command.date_expression is None
            and command.period_expression is not None
            and (normalized(command.period_expression) == "до конца недели"
                 or re.fullmatch(r"(?:до|к) (?:понедельник[ау]|вторник[ау]|сред[ые]|четверг[ау]|"
                                 r"пятниц[ые]|суббот[ые]|воскресень[яю])",
                                 normalized(command.period_expression)))):
        return command.model_copy(update={
            "date_expression": command.period_expression, "period_expression": None,
        })
    return command


class ProviderError(RuntimeError):
    """A safe, user-visible error; never includes API responses or credentials."""


class LLMProvider(Protocol):
    def parse_command(self, text: str, context: dict) -> ParsedCommand: ...


class IntentParser:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    def parse(self, text: str, context: dict) -> ParsedCommand:
        # Revalidate at the boundary even when a provider returns a Pydantic object.
        try:
            command = ParsedCommand.model_validate(self.provider.parse_command(text, context))
        except ValidationError as exc:
            raise ProviderError("Модель вернула некорректную команду. Переформулируйте запрос.") from exc
        return self._validate(command, text)

    def _validate(self, command: ParsedCommand, text: str, *, child=False) -> ParsedCommand:
        if (command.intent == Intent.CREATE_EVENT and command.event_type == EventType.MEETING
                and not command.time_expression and not command.end_time_expression):
            source = normalized(text)
            marker = re.search(r"\b(?:на весь день|весь день|без указания времени)\b", source)
            negated = re.search(r"\bне (?:на )?весь день\b|\bне без указания времени\b", source)
            if marker and not negated and not command.time_expressions:
                command = command.model_copy(update={"time_expression": marker[0]})
        if command.intent == Intent.DELETE_EVENT:
            quoted = [next(value for value in match.groups() if value is not None) for match in re.finditer(
                r'"([^"\n]+)"|«([^»\n]+)»|“([^”\n]+)”', text)]
            if len(quoted) >= 2:
                if len(quoted) > 12:
                    raise ProviderError("Укажите не больше 12 названий за один запрос.")
                command = command.model_copy(update={"delete_targets": quoted, "search_query": None, "title": None})
        if command.delete_targets:
            if command.intent != Intent.DELETE_EVENT or command.actions:
                raise ProviderError("Уточните отдельный запрос удаления.")
            supplied = " ".join(text.casefold().replace("ё", "е").split())
            for target in command.delete_targets:
                literal = " ".join(target.casefold().replace("ё", "е").split())
                if not literal or len(literal) > 500 or not re.search(r'(?<!\w)' + re.escape(literal) + r'(?!\w)', supplied):
                    raise ProviderError("Модель добавила неуказанное название для удаления.")
            command = command.model_copy(update={"search_query": None, "title": None})
        if command.actions:
            if command.intent != Intent.CREATE_EVENT or any(
                item.intent != Intent.CREATE_EVENT or item.actions or item.recurrence_expression or item.time_expressions
                for item in command.actions
            ):
                raise ProviderError("Уточните отдельные действия без вложенных списков и повторений.")
            command = command.model_copy(update={
                "actions": [self._validate(item, text, child=True) for item in command.actions],
            })
        if command.relative_anchor_expression and not re.search(
            r'(?<!\w)' + re.escape(normalized(command.relative_anchor_expression)) + r'(?!\w)', normalized(text)
        ):
            raise ProviderError("Модель добавила неуказанную связь между напоминаниями.")
        scope = explicit_recurrence_scope(text)
        if command.recurrence_scope is not None and command.recurrence_scope != scope:
            raise ProviderError("Модель добавила неуказанную область серии. Уточните запрос.")
        if command.intent in (Intent.UPDATE_EVENT, Intent.DELETE_EVENT) and scope:
            command = command.model_copy(update={"recurrence_scope": scope})
        if command.new_title and normalized(command.new_title) not in normalized(text):
            raise ProviderError("Модель добавила неуказанное новое название. Уточните запрос.")
        if command.recipient_expression and not re.search(
            r'(?<!\w)'+re.escape(normalized(command.recipient_expression))+r'(?!\w)',normalized(text),
        ):
            raise ProviderError('Модель добавила неуказанного адресата. Уточните запрос.')
        if command.intent in (Intent.DELETE_EVENT, Intent.COMPLETE_EVENT, Intent.UPDATE_EVENT):
            selection_text = text
            for expression in (command.date_expression, command.period_expression):
                if expression:
                    selection_text = re.sub(re.escape(expression), " ", selection_text, flags=re.I)
            index = ordinal_index(selection_text)
            if command.event_index is not None and command.event_index != index:
                raise ProviderError("Модель добавила неуказанный номер события. Уточните запрос.")
            if index is not None:
                if not 1 <= index <= 1000:
                    raise ProviderError("Номер события должен быть от 1 до 1000.")
                command = ParsedCommand.model_validate(command.model_dump() | {
                    "event_index": index, "event_reference": "last_listed",
                })
        if command.intent in (Intent.DELETE_EVENT, Intent.COMPLETE_EVENT):
            supplied = re.findall(r"\w+", normalized(text))
            for expression in (command.search_query, command.title):
                if expression and not all(word in supplied for word in re.findall(r"\w+", normalized(expression))):
                    raise ProviderError("Модель добавила неуказанное содержание события. Уточните запрос.")
        if (command.intent == Intent.CREATE_EVENT and command.date_expression is None
                and command.period_expression is None):
            typo = re.search(r"\b(?:завтр|помлезавтра|послезавтр)\b", text, re.I)
            if typo and not re.search(r"\bне\s+" + re.escape(typo[0]), text, re.I):
                command = command.model_copy(update={"date_expression": typo[0]})
        user_tokens = temporal_tokens(text)
        if (command.intent in (Intent.CREATE_EVENT, Intent.UPDATE_EVENT)
                and not command.actions and not command.time_expressions and not command.end_time_expression
                and command.time_expression in (None, "01:00", "1:00", "13:00")
                and re.search(r"\bв час\b", normalized(text))
                and not re.search(r"\b(?:утра|утром|дня|днем|вечера|вечером|ночи|ночью)\b|\d{1,2}:\d{2}", normalized(text))):
            # Preserve the user's ambiguity instead of accepting an invented AM/PM.
            command = command.model_copy(update={"time_expression": "в час"})
        expressions = [(field, getattr(command, field)) for field in (
            "date_expression", "time_expression", "end_time_expression",
            "period_expression", "shift_expression", "recurrence_expression",
        )]
        expressions.extend(("time_expressions", value) for value in command.time_expressions or [])
        for field, expression in expressions:
            if not expression:
                continue
            tokens = temporal_tokens(expression)
            if field == 'recurrence_expression':
                base, limits = recurrence_fragments(expression)
                if limits:
                    try:
                        parse_recurrence(base)
                    except RecurrenceError:
                        pass  # A literal unsupported rule still reaches domain clarification below.
                    else:
                        fragments = [temporal_tokens(part) for part in [base,*limits]]
                        if all(part and any(user_tokens[i:i+len(part)] == part
                               for i in range(len(user_tokens)-len(part)+1)) for part in fragments):
                            continue
            if not tokens or not any(
                user_tokens[index:index + len(tokens)] == tokens
                for index in range(len(user_tokens) - len(tokens) + 1)
            ):
                logger.debug("NLU temporal guard mismatch: field=%s", field)
                raise ProviderError("Модель добавила неуказанную дату или время. Переформулируйте запрос.")
        if command.time_expressions:
            # A range spoken by the user cannot become a batch merely because NLU
            # put its endpoints in the plural field. Reuse the supported clock grammar.
            clock = TIME_PATTERN.pattern
            interval = re.search(
                rf"\b(?:с {clock} до {clock}|(?:в любое время )?между {clock} и {clock})(?!\w)",
                normalized(text),
            )
            if interval:
                command = command.model_copy(update={
                    "time_expression": interval[0], "time_expressions": None,
                    "end_time_expression": None,
                })
        if command.intent == Intent.CREATE_EVENT and not command.actions and not child:
            relatives = command.time_expressions or []
            if len(relatives) > 1 and all(re.match(r"через\b", normalized(value)) for value in relatives):
                actions, cursor = [], 0
                for value in relatives:
                    match = re.search(re.escape(normalized(value)), normalized(text)[cursor:])
                    if match is None:
                        raise ProviderError("Уточните интервалы каждого напоминания.")
                    between = normalized(text)[cursor:cursor + match.start()]
                    connector = re.search(r"(?:потом|затем)(?: еще)?|после (?:первого|предыдущего) напоминания", between)
                    actions.append(command.model_copy(update={"actions": None, "time_expressions": None,
                        "time_expression": None, "date_expression": value,
                        "relative_anchor_expression": connector[0] if connector and actions else None}))
                    cursor += match.end()
                command = command.model_copy(update={"actions": actions, "time_expressions": None})
            elif len(re.findall(r"\bнапомни(?:ть)?\b", normalized(text))) > 1:
                raise ProviderError("Уточните каждое напоминание отдельно: модель объединила разные действия.")
        if command.actions:
            actions, cursor = [], 0
            for item in command.actions:
                expression = item.date_expression or item.time_expression or ""
                suffix = re.fullmatch(r"(через .+?) (после (?:первого|предыдущего) напоминания)", normalized(expression))
                if suffix:
                    expression = suffix[1]
                if re.match(r"через\b", normalized(expression)):
                    match = re.search(re.escape(normalized(expression)), normalized(text)[cursor:])
                    if match is None:
                        raise ProviderError("Уточните порядок относительных напоминаний.")
                    between = normalized(text)[cursor:cursor + match.start()]
                    following = normalized(text)[cursor + match.end():]
                    suffix_connector = re.match(r"\s+(после (?:первого|предыдущего) напоминания)\b", following)
                    connector = re.search(r"(?:потом|затем)(?: еще)?|после (?:первого|предыдущего) напоминания", between)
                    # The relation is grounded in the actual span between actions,
                    # including when NLU omitted this structured field.
                    literal = (suffix_connector[1] if suffix_connector else connector[0] if connector else None) if actions else None
                    if (item.relative_anchor_expression and normalized(item.relative_anchor_expression)
                            not in between + (suffix_connector[0] if suffix_connector else "")):
                        raise ProviderError("Уточните связь между относительными напоминаниями.")
                    item = item.model_copy(update={"date_expression": expression,
                        "time_expression": item.time_expression if item.date_expression else None, "relative_anchor_expression": literal})
                    cursor += match.end() + (suffix_connector.end() if suffix_connector else 0)
                actions.append(item)
            command = command.model_copy(update={"actions": actions})
        if (command.intent == Intent.UPDATE_EVENT and command.new_title and command.date_expression
                and not command.period_expression and not command.time_expression
                and not command.end_time_expression and not command.shift_expression):
            rename = re.fullmatch(r"(?:замени|переименуй) (.+?) на "
                + re.escape(normalized(command.new_title)) + r"[.!]?", normalized(text))
            if rename and normalized(command.date_expression) in rename[1]:
                command = command.model_copy(update={
                    "period_expression": command.date_expression, "date_expression": None,
                })
        return normalize_command(command)
