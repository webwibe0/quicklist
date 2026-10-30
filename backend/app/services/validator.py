from app.schemas.commands import (
    CompleteEventCommand,
    CreateEventCommand,
    DeleteEventCommand,
    EventType,
    Intent,
    ListEventsCommand,
    ParsedCommand,
    SearchEventsCommand,
    UpdateEventCommand,
)
from app.temporal.resolver import TemporalResult


def normalize_action(command: ParsedCommand, temporal: TemporalResult) -> ParsedCommand:
    """Filter-only requests are listings, after dialogue context has been merged."""
    has_period = bool((command.period_expression or "").strip()) or (
        temporal.period_start is not None and temporal.period_end is not None
    )
    if (command.intent == Intent.SEARCH_EVENTS and not (command.search_query or "").strip()
            and has_period and not (command.title or "").strip()
            and not (command.time_expression or "").strip()):
        return command.model_copy(update={"intent": Intent.LIST_EVENTS, "search_query": None})
    return command


class CommandValidator:
    def validate(self, command: ParsedCommand, temporal: TemporalResult, raw_text: str, target=None):
        missing = list(temporal.issues)
        title = (command.title or "").strip()
        if temporal.interval_requested:
            if temporal.event_time is None:
                missing.append("time")
            if temporal.end_time is None:
                missing.append("end_time")
        if command.intent == Intent.CREATE_EVENT:
            if "recurrence" in temporal.issues:
                return None, missing
            if not command.event_type:
                missing.append("event_type")
            if not title:
                missing.append("title")
            if command.event_type in (EventType.REMINDER, EventType.MEETING):
                if temporal.event_date is None:
                    missing.append("date")
                if temporal.event_time is None and not (
                    command.event_type == EventType.MEETING and temporal.time_unspecified
                ):
                    missing.append("time")
            if temporal.event_time and temporal.event_date is None:
                missing.append("date")
            if command.recurrence_expression and temporal.event_date is None:
                missing.append("date")
            if command.period_expression and temporal.event_date is None:
                missing.append("date")
            if not missing:
                return CreateEventCommand(
                    event_type=command.event_type, title=title,
                    event_date=temporal.event_date, event_time=temporal.event_time,
                    end_time=temporal.end_time,
                    raw_user_text=raw_text,
                    recurrence_expression=command.recurrence_expression,
                ), []
        elif command.intent in (Intent.DELETE_EVENT, Intent.COMPLETE_EVENT):
            if target is None:
                missing.append("event_reference")
            if not missing:
                cls = DeleteEventCommand if command.intent == Intent.DELETE_EVENT else CompleteEventCommand
                return cls(event_id=target.id), []
        elif command.intent == Intent.UPDATE_EVENT:
            title_only = command.new_title and not any((command.date_expression,
                command.time_expression, command.end_time_expression, command.shift_expression))
            if target is None:
                missing.append("event_reference")
            if temporal.event_date is None and not title_only:
                missing.append("date")
            if temporal.event_time is None and not title_only and not (target is not None and target.type == "meeting"
                                                    and temporal.time_unspecified):
                missing.append("time")
            if not missing:
                return UpdateEventCommand(
                    event_id=target.id, event_date=temporal.event_date,
                    event_time=temporal.event_time, end_time=temporal.end_time,
                    title=command.new_title,
                ), []
        elif command.intent == Intent.LIST_EVENTS:
            start = temporal.period_start or temporal.event_date
            end = temporal.period_end or temporal.event_date
            if not start or not end:
                missing.append("period")
            if not missing:
                return ListEventsCommand(start=start, end=end, event_type=command.event_type,
                                         temporal_direction=temporal.search_direction), []
        elif command.intent == Intent.SEARCH_EVENTS:
            query = (command.search_query or "").strip()
            if not query:
                missing.append("search_query")
            if not missing:
                return SearchEventsCommand(
                    query=query, start=temporal.period_start or temporal.event_date,
                    end=temporal.period_end or temporal.event_date, event_type=command.event_type,
                ), []
        return None, list(dict.fromkeys(missing))
