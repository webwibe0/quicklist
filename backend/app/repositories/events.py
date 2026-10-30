import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Event
from app.schemas.commands import (
    CreateEventCommand,
    Intent,
    ListEventsCommand,
    SearchEventsCommand,
    UpdateEventCommand,
)
from app.temporal.intervals import scheduled_interval


def exact_duplicate(command, event):
    def title_key(value):
        return " ".join(value.casefold().replace("ё", "е").split())
    return (event.status == "pending" and event.type == command.event_type.value.lower()
            and event.event_date == command.event_date and event.event_time == command.event_time
            and event.end_time == command.end_time and title_key(event.title) == title_key(command.title))


class EventRepository:
    def __init__(self, session: Session, limit: int):
        self.session = session
        self.limit = limit

    def daily_candidates(self, day):
        """Owned active occurrences; the day plan also keeps earlier unfinished work."""
        return list(self.session.scalars(select(Event).where(
            Event.status == 'pending', or_(Event.event_date <= day, Event.event_date.is_(None)),
        ).order_by(Event.event_date.asc().nulls_last(), Event.event_time.asc().nulls_last(), Event.id)))

    def get_context_events(self, ids):
        if not ids:
            return []
        return list(self.session.scalars(select(Event).where(
            Event.id.in_(ids), Event.status == "pending",
        ).with_for_update().execution_options(populate_existing=True)))

    def get_action_events(self, ids, intent):
        if not ids:
            return []
        query = select(Event).where(Event.id.in_(ids), Event.status != "deleted")
        if intent == Intent.COMPLETE_EVENT:
            query = query.where(Event.status.in_(["pending", "completed"]))
        return list(self.session.scalars(query.with_for_update().execution_options(populate_existing=True)))

    def mutation_candidates(self, command, temporal):
        query = select(Event).where(Event.status != "deleted")
        if command.intent == Intent.COMPLETE_EVENT:
            query = query.where(Event.status.in_(["pending", "completed"]))
        if command.event_type:
            query = query.where(Event.type == command.event_type.value.lower())
        # UPDATE date/time are destinations; period is an explicit existing-row filter.
        if command.intent == Intent.UPDATE_EVENT:
            query = query.where(Event.status == "pending")
            if temporal.period_start:
                query = query.where(Event.event_date >= temporal.period_start)
            if temporal.period_end:
                query = query.where(Event.event_date <= temporal.period_end)
            return self.session.scalars(query.order_by(
                Event.event_date.asc().nulls_last(), Event.event_time.asc().nulls_last(), Event.id,
            ).with_for_update().execution_options(populate_existing=True))
        start, end = temporal.period_start or temporal.event_date, temporal.period_end or temporal.event_date
        if start:
            query = query.where(Event.event_date >= start)
        if end:
            query = query.where(Event.event_date <= end)
        if temporal.event_time is not None:
            query = query.where(Event.event_time == temporal.event_time)
        if temporal.end_time is not None:
            query = query.where(Event.end_time == temporal.end_time)
        return self.session.scalars(query.order_by(
            Event.event_date.asc().nulls_last(), Event.event_time.asc().nulls_last(), Event.id,
        ).with_for_update().execution_options(populate_existing=True))

    def change_status(self, command, intent):
        if intent == Intent.DELETE_EVENT and command.recurrence_series_id is not None:
            candidates = self.get_series_events(command.recurrence_series_id, intent)
        else:
            candidates = self.get_action_events([command.event_id], intent)
        if not candidates:
            return []
        status = "deleted" if intent == Intent.DELETE_EVENT else "completed"
        with self.session.begin_nested():
            for event in candidates:
                event.status = status
            self.session.flush()
        return candidates

    def delete_selection(self, command):
        events=self.get_action_events(command.event_ids,Intent.DELETE_EVENT)
        indexed={event.id:event for event in events}
        if set(indexed)!=set(command.event_ids):
            return []
        selected=[indexed[event_id] for event_id in command.event_ids]
        with self.session.begin_nested():
            for event in selected:
                event.status='deleted'
            self.session.flush()
        return selected

    def get_series_events(self, series_id, intent):
        query = select(Event).where(Event.recurrence_series_id == series_id, Event.status != "deleted")
        if intent == Intent.UPDATE_EVENT:
            # Keep the existing update policy: completed/cancelled history is immutable.
            query = query.where(Event.status == "pending")
        return list(self.session.scalars(query.order_by(
            Event.event_date, Event.event_time.asc().nulls_last(), Event.id,
        ).with_for_update().execution_options(populate_existing=True)))

    def update_batch(self, commands):
        with self.session.begin_nested():
            return [self.update(command) for command in commands]

    def update(self, command: UpdateEventCommand) -> Event:
        event = self.session.get(Event, command.event_id)
        event.event_date = command.event_date
        event.event_time = command.event_time
        event.end_time = command.end_time
        if command.title is not None:
            event.title = command.title
        self.session.flush()
        return event

    def create(self, command: CreateEventCommand) -> Event:
        event = Event(
    type=command.event_type.value.lower(), title=command.title,
    event_date=command.event_date, event_time=command.event_time,
    end_time=command.end_time,
    raw_user_text=command.raw_user_text,
    recurrence_rule=command.recurrence_rule,
    recurrence_series_id=command.recurrence_series_id,
    recurrence_until=command.recurrence_until,
    source="user", type_inferred=False,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def find_duplicate(self, command: CreateEventCommand) -> Event | None:
        return next(iter(self.find_duplicates(command)), None)

    def find_duplicates(self, command: CreateEventCommand) -> list[Event]:
        candidates = self.session.scalars(select(Event).where(
            Event.type == command.event_type.value.lower(), Event.status == "pending",
            Event.event_date == command.event_date, Event.event_time == command.event_time,
            Event.end_time == command.end_time,
        ))
        return [event for event in candidates if exact_duplicate(command, event)]
    def find_conflicts(
        self,
        command: CreateEventCommand | UpdateEventCommand,
        *,
        event_type: str | None = None,
        exclude_event_id=None,
        exclude_event_ids=(),
    ) -> list[Event]:
        resolved_type = (
            command.event_type.value.lower()
            if isinstance(command, CreateEventCommand)
            else event_type
        )

        if (
            resolved_type != "meeting"
            or command.event_date is None
            or command.event_time is None
        ):
            return []

        query = select(Event).where(
            Event.type == "meeting",
            Event.status == "pending",
            Event.event_date.between(
                command.event_date - timedelta(days=int(command.event_date > date.min)),
                command.event_date + timedelta(days=int(command.event_date < date.max)),
            ),
            Event.event_time.is_not(None),
        )

        if exclude_event_id is not None:
            query = query.where(Event.id != exclude_event_id)
        if exclude_event_ids:
            query = query.where(Event.id.not_in(exclude_event_ids))

        candidates = self.session.scalars(
            query.order_by(Event.event_time, Event.id)
        )

        conflicts = []
        start, end = scheduled_interval(command.event_date, command.event_time, command.end_time)
        for event in candidates:
            other_start, other_end = scheduled_interval(event.event_date, event.event_time, event.end_time)
            if command.end_time is not None and event.end_time is not None:
                if start < other_end and other_start < end:
                    conflicts.append(event)
                continue

            if other_start == start:
                conflicts.append(event)

        return conflicts

    def create_batch(self, commands: list[CreateEventCommand]) -> list[Event]:
        # A savepoint also protects callers that catch errors inside an outer transaction.
        with self.session.begin_nested():
            return [self.create(command) for command in commands]

    def list_by_period(self, command: ListEventsCommand) -> list[Event]:
        query = select(Event).where(
            Event.status != "deleted" if command.include_inactive or command.status else Event.status == "pending")
        query = self.search_filters(query, command)
        if command.recurrence_required is True:
            # Filter first, then one visible occurrence per real series/status/title.
            ranked = query.add_columns(func.row_number().over(
                partition_by=(Event.recurrence_series_id, Event.status, Event.title),
                order_by=(Event.event_date, Event.event_time.asc().nulls_last(), Event.id),
            ).label("occurrence_rank")).subquery()
            query = select(Event).join(ranked, Event.id == ranked.c.id).where(ranked.c.occurrence_rank == 1)
        return list(self.session.scalars(query.order_by(
            Event.event_date, Event.event_time.asc().nulls_last(), Event.id,
        ).limit(self.limit + 1)))

    def search(self, command: SearchEventsCommand, *, candidate_limit=None) -> list[Event]:
        tokens = re.findall(r"[\w]+", command.query.lower())[:12]
        if not tokens:
            return []
        language = literal_column("'russian'")
        vector = func.to_tsvector(language, Event.title)
        tsquery = func.plainto_tsquery(language, command.query)
        rank = func.ts_rank_cd(vector, tsquery)
        similarities = [func.word_similarity(token, func.lower(Event.title)) for token in tokens]
        fuzzy = and_(*(or_(
            Event.title.icontains(token, autoescape=True), similarity >= 0.45,
        ) for token, similarity in zip(tokens, similarities)))
        query = select(Event).where(or_(vector.op("@@")(tsquery), fuzzy), Event.status != "deleted")
        query = self.search_filters(query, command)
        similarity_rank = sum(similarities) / len(similarities)
        return list(self.session.scalars(query.order_by(
            (rank + similarity_rank).desc(), Event.event_date.desc().nulls_last(), Event.id,
        ).limit(candidate_limit or self.limit + 1)))

    def search_filters(self, query, command):
        query = query.where(Event.status != "deleted")
        if not getattr(command, "include_unscheduled", False):
            if command.start:
                query = query.where(Event.event_date >= command.start)
            if command.end:
                query = query.where(Event.event_date <= command.end)
        if command.event_type:
            query = query.where(Event.type == command.event_type.value.lower())
        if command.status:
            query = query.where(Event.status == command.status)
        if command.recurrence_required is True:
            query = query.where(Event.recurrence_rule.is_not(None), Event.recurrence_rule != "",
                                Event.recurrence_series_id.is_not(None))
        elif command.recurrence_required is False:
            query = query.where(Event.recurrence_rule.is_(None), Event.recurrence_series_id.is_(None))
        return self._with_direction(query, command.temporal_direction, command.as_of,
                                    include_inactive=bool(command.status) or getattr(command, "include_inactive", False))

    def semantic(self, command, vector, profile, settings):
        from app.core.search_content import search_content
        from app.models.search import EmbeddingCache, EventSearchDocument
        distance = EmbeddingCache.embedding.cosine_distance(vector)
        query = select(Event, EmbeddingCache).join(EventSearchDocument, EventSearchDocument.current_event_condition()).join(
            EmbeddingCache,
            (EmbeddingCache.content_hash == EventSearchDocument.content_hash)
            & (EmbeddingCache.text_version == EventSearchDocument.text_version),
        ).where(
            EmbeddingCache.model == profile.model,
            EmbeddingCache.dimensions == profile.dimensions,
            EmbeddingCache.text_version == profile.text_version,
            EmbeddingCache.status == "ready",
            distance <= 1 - settings.semantic_min_similarity,
        )
        query = self.search_filters(query, command)
        rows = self.session.execute(query.order_by(
            distance, Event.event_date.asc().nulls_last(), Event.event_time.asc().nulls_last(), Event.id,
        ).limit(settings.semantic_candidate_limit))
        events = []
        for event, cached in rows:
            try:
                cached.assert_content(search_content(event.title))
            except ValueError:
                continue
            events.append(event)
        return events

    @staticmethod
    def _with_direction(query, direction, as_of, *, include_inactive=False):
        if direction:
            if as_of is None or as_of.tzinfo is None:
                raise ValueError("A timezone-aware search boundary is required")
            local = as_of.astimezone(ZoneInfo("Europe/Moscow"))
            today, clock = local.date(), local.time().replace(tzinfo=None)
            # No made-up midnight for a same-day event without a clock time.
            boundary = func.coalesce(Event.end_time, Event.event_time)
            end_date = case((Event.end_time < Event.event_time, Event.event_date + 1), else_=Event.event_date)
            if direction == "past":
                query = query.where(or_(
                    end_date < today,
                    and_(end_date == today, boundary <= clock),
                ))
                if not include_inactive:
                    query = query.where(Event.status != "cancelled")
            else:
                query = query.where(or_(
                    Event.event_date > today,
                    and_(Event.event_date == today, Event.event_time > clock),
                ))
                if not include_inactive:
                    query = query.where(Event.status == "pending")
        return query
