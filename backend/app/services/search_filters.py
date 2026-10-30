"""Literal search constraints; no model-enriched retrieval facts or identities."""
import re
from datetime import date

from app.schemas.commands import Intent
from app.services.search_guard import requested_status


def literal_query(text, parsed=None):
    # A bare leading UI type is not a participant/object. Preserve quoted title
    # text and occurrences inside the actual query; do not weaken the guard.
    result = re.sub(
        r'^\s*(?:найди|найти|покажи|показать|поищи)\s+(?:(?:мою|мое|моё)\s+)?'
        r'(?:задачу|встречу|напоминание|событие)\b', ' ', text, flags=re.I,
    )
    if parsed is not None:
        for expression in (parsed.period_expression, parsed.date_expression, parsed.time_expression):
            if expression:
                result = re.sub(re.escape(expression), ' ', result, flags=re.I)
        # The NLU guard permits equivalent literal dates (DD.MM.YYYY / ISO).
        # Remove equivalent typed temporal tokens too; dates belong to SQL filters,
        # not the guard's explicit-number constraints on event titles.
        from app.ai.provider import temporal_tokens
        expected = [token for expression in (parsed.period_expression, parsed.date_expression,
                    parsed.time_expression) if expression for token in temporal_tokens(expression)
                    if isinstance(token, tuple)]
        pattern = r'(?<!\w)(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}(?:\.\d{4})?|\d{1,2}:\d{2})(?!\w|[.:]\d)'
        for match in reversed(list(re.finditer(pattern, result))):
            tokens = temporal_tokens(match[0])
            if len(tokens) == 1 and isinstance(tokens[0], tuple) and tokens[0] in expected:
                result = result[:match.start()] + ' ' + result[match.end():]
    # Strip only interface/filter words. Preserve case, negations, actions and names.
    result = re.sub(
        r'\b(?:найди|найти|покажи|показать|поищи|мои|мою|моё|мое|все|всех|всю|'
        r'пожалуйста|только|события|событий|задачи|задач|встречи|встреч|напоминания|напоминаний|'
        r'повторяющ\w*|неповторяющ\w*|отмен[её]нн\w*|выполненн\w*|заверш[её]нн\w*|активн\w*)\b',
        ' ', result, flags=re.I,
    )
    return ' '.join(result.strip(' .,!?:;').split())


def filters_from_text(text):
    recurring = None
    if re.search(r'\bповторяющ\w*\b', text, re.I):
        recurring = True
    if re.search(r'\b(?:неповторяющ\w*|без повторений)\b', text, re.I):
        recurring = False
    return dict(status=requested_status(text), recurrence_required=recurring)


def normalize_filter_listing(parsed, temporal, text):
    if parsed.intent not in (Intent.SEARCH_EVENTS, Intent.LIST_EVENTS):
        return parsed
    filters = filters_from_text(text)
    if any(value is not None for value in filters.values()) and not literal_query(text, parsed):
        if not temporal.period_start and not temporal.period_end and not temporal.issues:
            temporal.period_start, temporal.period_end = date.min, date.max
        return parsed.model_copy(update={'intent': Intent.LIST_EVENTS, 'search_query': None})
    return parsed
