"""Explicit scope words only. The database, never the model, identifies a series."""
import re

SCOPE_PATTERNS = {
    "occurrence": r"\bтолько (?:эту(?: (?:встречу|задачу))?|это(?: (?:событие|напоминание))?)\b",
    "series": r"\b(?:всю серию|всей серии|все повторения|все такие (?:встречи|события|напоминания|задачи))\b",
}


def explicit_recurrence_scope(text):
    text = " ".join(text.casefold().replace("ё", "е").split())
    # A quoted event title is not permission to mutate an entire series.
    text = text[1:-1] if re.fullmatch(r'«[^»]+»|"[^"]+"', text) else re.sub(r'«[^»]*»|"[^"]*"', "", text)
    scopes = {scope for scope, pattern in SCOPE_PATTERNS.items() for match in re.finditer(pattern, text)
              if not re.search(r"\b(?:не|кроме)\s*$", text[:match.start()])}
    return scopes.pop() if len(scopes) == 1 else None
