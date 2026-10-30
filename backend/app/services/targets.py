"""Resolve mutations from explicit filters or visible conversation scope, never model IDs."""
import re

from app.schemas.commands import Intent, SearchEventsCommand


def words(text):
    return re.findall(r"\w+", text.casefold().replace("ё", "е"))


def word_score(left, right):
    if left == right:
        return 1.0
    if min(len(left), len(right)) < 4 or not left.isalpha() or not right.isalpha():
        return 0.0
    if any(left==right+ending or right==left+ending for ending in ('а','я','у','ю','е','ы','и','ом','ем')):
        return .9
    # A conservative inflection key, not a general Russian lemmatizer.
    endings = r"(?:иями|ями|ами|ого|ему|ому|ить|ать|яет|ает|ила|или|ала|али|ой|ей|ом|ем|ит|ил|ет|ал|ую|ая|ые|ый|а|я|ы|и|у|ю|е|ь)$"
    def stem(value):
        nominal = re.sub(r'(?:ение|ения|ению|ением|ении)$','',value)
        if nominal != value:
            return nominal.replace('жд','д')
        return re.sub(endings,'',value)
    a,b = stem(left),stem(right)
    if a == b and len(a) >= 4:
        return 0.9
    # At most one inserted, removed or replaced character, only for substantial words.
    if min(len(left), len(right)) < 5 or abs(len(left) - len(right)) > 1:
        return 0.0
    if len(left) == len(right):
        changed = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(changed) == 1:
            return 0.8
        if len(changed) == 2:
            a, b = changed
            if b == a + 1 and left[a] == right[b] and left[b] == right[a]:
                return 0.8
        return 0.0
    short, long = sorted((left, right), key=len)
    return 0.8 if any(long[:i] + long[i + 1:] == short for i in range(len(long))) else 0.0


def title_score(query, title, *, fuzzy=True):
    requested, stored = words(query), words(title)
    if set(requested) & {"не", "без"} != set(stored) & {"не", "без"}:
        return 0.0
    if not requested:
        return 1.0
    scores = [max((word_score(token, other) if fuzzy else float(token == other)
                   for other in stored), default=0) for token in requested]
    return sum(scores) / len(scores) if all(scores) else 0.0


class EventTargetResolver:
    def __init__(self, repository, search=None):
        self.repository = repository
        self.search = search

    def needs_selection(self, command, event):
        query = (
    command.title
    if command.intent == Intent.UPDATE_EVENT and command.title
    else command.search_query or command.title or ""
)
        if not query or title_score(query, event.title, fuzzy=False) == 1:
            return False
        # Only strong derivational UPDATE references bypass selection. A typo or
        # ordinary fuzzy inflection still requires the existing explicit choice.
        requested, stored = words(query), words(event.title)
        derivation = any(re.search(r"(?:ение|ения|ению|ением|ении)$", token)
            and any(word_score(token, other) >= .9 for other in stored) for token in requested)
        strong = all(max((word_score(token, other) for other in stored), default=0) >= .9 for token in requested)
        return not (command.intent == Intent.UPDATE_EVENT and derivation and strong)

    def resolve(self, command, temporal, context, text, candidate_ids=None, *, independent=False):
        reference = command.event_reference
        if command.intent == Intent.UPDATE_EVENT:
            explicit_target = bool(command.search_query or command.title)
            reference = reference or (
                None if independent else
                "last_listed" if explicit_target and context.last_listed_events else
                None if explicit_target else "last_created"
            )
        elif (not command.search_query and not command.title
              and re.search(r"\b(?:это|этот|эту|е[её])\b", text, re.I)):
            reference = context.last_event_scope or ("last_listed" if context.last_listed_events else "last_created")
        if command.event_index is not None:
            ids = candidate_ids if candidate_ids is not None else [event.id for event in context.last_listed_events]
            index = command.event_index - 1
            ids = ids[index:index + 1]
        elif candidate_ids is not None:
            ids = candidate_ids
        else:
            if not reference and not (command.search_query or command.title or command.period_expression
                                      or temporal.event_date):
                reference = context.last_event_scope or (
                    "last_listed" if context.last_listed_events else
                    "last_created" if context.last_created_event else None
                )
            if reference:
                snapshots = context.last_listed_events if reference == "last_listed" else (
                    [context.last_created_event] if context.last_created_event else []
                )
                ids = [event.id for event in snapshots]
            else:
                ids = None
        if ids is not None:
            fetch = (self.repository.get_context_events if command.intent == Intent.UPDATE_EVENT
                     else lambda ids: self.repository.get_action_events(ids, command.intent))
            indexed = {event.id: event for event in fetch(ids)}
            candidates = [indexed[event_id] for event_id in ids if event_id in indexed]
        else:
            candidates = self.repository.mutation_candidates(command, temporal)
        query = (
    command.title
    if command.intent == Intent.UPDATE_EVENT and command.title
    else command.search_query or command.title or ""
)
        matches = []
        for event in candidates:
            if command.event_type and event.type != command.event_type.value.lower():
                continue
            score = title_score(query, event.title, fuzzy=command.intent != Intent.COMPLETE_EVENT)
            if not score:
                continue
            if command.intent == Intent.UPDATE_EVENT:
                start, end = temporal.period_start, temporal.period_end
            else:
                start = temporal.period_start or temporal.event_date
                end = temporal.period_end or temporal.event_date
            if start and (event.event_date is None or event.event_date < start):
                continue
            if end and (event.event_date is None or event.event_date > end):
                continue
            if command.intent != Intent.UPDATE_EVENT:
                if temporal.event_time is not None and event.event_time != temporal.event_time:
                    continue
                if temporal.end_time is not None and event.end_time != temporal.end_time:
                    continue
            matches.append((score, event))
        if not matches and ids is None and command.intent == Intent.UPDATE_EVENT and query and self.search:
            # Ground retrieval in the literal user reference, never model-added facts.
            original = context.pending_action.raw_user_text if context.pending_action else text
            if re.search(r'(?<!\w)' + re.escape(" ".join(query.casefold().split())) + r'(?!\w)',
                         " ".join(original.casefold().split())):
                found = self.search.execute(SearchEventsCommand(
                    query=query, raw_query=query, status="pending", event_type=command.event_type,
                    start=temporal.period_start, end=temporal.period_end,
                ))
                # Search proposes only. Re-fetch under the ordinary owner/status/lock guards.
                selected = [event.id for event in found if ids is None or event.id in ids]
                for event in self.repository.get_context_events(selected):
                    if event.status != "pending" or (command.event_type and event.type != command.event_type.value.lower()):
                        continue
                    if temporal.period_start and (event.event_date is None or event.event_date < temporal.period_start):
                        continue
                    if temporal.period_end and (event.event_date is None or event.event_date > temporal.period_end):
                        continue
                    matches.append((0.5, event))
        # ponytail: rank the filtered prototype dataset in memory; use indexed retrieval at scale.
        matches.sort(key=lambda item: item[0], reverse=True)
        if command.recurrence_scope == "series" and matches:
            series_id = matches[0][1].recurrence_series_id
            if series_id is not None and all(event.recurrence_series_id == series_id for _, event in matches):
                return [matches[0][1]]
        return [event for _, event in matches[:self.repository.limit + 1]]
