from datetime import datetime
from uuid import uuid4

from app.repositories.events import EventRepository
from app.schemas.commands import (
    BatchCreateEventCommand,
    CompleteEventCommand,
    CreateEventCommand,
    CreateSeriesCommand,
    DeleteEventCommand,
    DeleteSelectionCommand,
    Intent,
    ListEventsCommand,
    SearchEventsCommand,
    UpdateEventCommand,
    UpdateSeriesCommand,
    creation_commands,
)
from app.temporal.intervals import scheduled_duration
from app.temporal.recurrence import (
    MAX_RECURRENCE_OCCURRENCES,
    InvalidRecurrenceError,
    RecurrenceLimitError,
    generate_recurrence_dates,
    parse_recurrence,
)


class CreateEventUseCase:
    def __init__(self, repository: EventRepository):
        self.repository = repository

    @staticmethod
    def expand(command: CreateEventCommand, *, series_id=None) -> list[CreateEventCommand]:
        """Pure preview: no DB writes, no subsequent regeneration when persisted."""
        if not command.recurrence_expression:
            return [command]
        if command.event_date is None:
            raise InvalidRecurrenceError("Для повторений нужна первая дата. Уточните её.")
        rule = parse_recurrence(command.recurrence_expression)
        dates = generate_recurrence_dates(command.event_date, rule)
        weekday_codes = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
        parts = [f"FREQ={rule.frequency.upper()}"]
        if rule.interval != 1:
            parts.append(f'INTERVAL={rule.interval}')
        if rule.weekdays:
            parts.append("BYDAY=" + ",".join(weekday_codes[day] for day in rule.weekdays))
        if rule.month_day is not None:
            parts.append(f"BYMONTHDAY={rule.month_day}")
        if rule.count is not None:
            parts.append(f"COUNT={rule.count}")
        if rule.until is not None:
            parts.append(f"UNTIL={rule.until:%Y%m%d}")
        series_id = series_id or uuid4()
        return [command.model_copy(update={
            "event_date": day, "recurrence_expression": None,
            "recurrence_rule": ";".join(parts), "recurrence_series_id": series_id,
            "recurrence_until": rule.until,
        }) for day in dates]

    @classmethod
    def prepare(cls, command):
        if isinstance(command, CreateSeriesCommand):
            return command
        templates = creation_commands(command)
        if not any(item.recurrence_expression for item in templates):
            return command
        series_id = uuid4()
        occurrences = [event for template in templates for event in cls.expand(template, series_id=series_id)]
        if len(occurrences) > MAX_RECURRENCE_OCCURRENCES:
            raise RecurrenceLimitError("Слишком много повторений: максимум 200 событий за запрос.")
        return CreateSeriesCommand(occurrences=occurrences)

    def execute(self, command):
        command = self.prepare(command)
        if isinstance(command, (BatchCreateEventCommand, CreateSeriesCommand)):
            return self.repository.create_batch(creation_commands(command))
        return [self.repository.create(command)]


class UpdateSeriesUseCase:
    def __init__(self, repository: EventRepository):
        self.repository = repository

    def expand(self, command, target, *, change_time, explicit_end, relative_shift=False):
        events = self.repository.get_series_events(target.recurrence_series_id, Intent.UPDATE_EVENT)
        if not events:
            raise InvalidRecurrenceError("Серия больше недоступна для изменения.")
        if len(events) > MAX_RECURRENCE_OCCURRENCES:
            raise RecurrenceLimitError("Серия слишком большая для одного изменения.")
        if command.event_date is None or target.event_date is None:
            raise InvalidRecurrenceError("Для изменения серии нужна дата выбранного события.")
        delta = command.event_date - target.event_date
        moment_delta = (datetime.combine(command.event_date, command.event_time)
                        - datetime.combine(target.event_date, target.event_time)) if relative_shift else None
        updates = []
        for event in events:
            try:
                if event.event_date is None or (relative_shift and event.event_time is None):
                    raise InvalidRecurrenceError("Для сдвига каждого события серии нужны дата и время.")
                day = event.event_date + delta
                clock = command.event_time if change_time else event.event_time
                if relative_shift:
                    moment = datetime.combine(event.event_date, event.event_time) + moment_delta
                    day, clock = moment.date(), moment.time()
                end = command.end_time if explicit_end else event.end_time
                if change_time and not explicit_end and event.end_time is not None:
                    duration = scheduled_duration(event.event_date, event.event_time, event.end_time)
                    finish = datetime.combine(day, clock) + duration
                    end = finish.time()
                if end is not None and (clock is None or end == clock):
                    raise InvalidRecurrenceError("Окончание должно быть позже начала каждого события серии.")
                if (explicit_end and end is not None and end < clock
                        and not (command.end_time < command.event_time)):
                    raise InvalidRecurrenceError("У части серии окончание раньше начала. Уточните время всей серии.")
            except OverflowError as exc:
                raise InvalidRecurrenceError("Сдвиг серии выходит за допустимые даты.") from exc
            updates.append(UpdateEventCommand(event_id=event.id, event_date=day, event_time=clock,
                                              end_time=end, title=command.title))
        return UpdateSeriesCommand(recurrence_series_id=target.recurrence_series_id, updates=updates)


class ListEventsUseCase:
    def __init__(self, repository: EventRepository):
        self.repository = repository

    def execute(self, command: ListEventsCommand):
        return self.repository.list_by_period(command)


class SearchEventsUseCase:
    def __init__(self, repository: EventRepository):
        self.repository = repository

    def execute(self, command: SearchEventsCommand):
        return self.repository.search(command)


class ActionRouter:
    def __init__(self, repository: EventRepository, *, search=None):
        self.repository = repository
        self.create = CreateEventUseCase(repository)
        self.update_series = UpdateSeriesUseCase(repository)
        self.list = ListEventsUseCase(repository)
        self.search = search if search is not None else SearchEventsUseCase(repository)

    def execute(self, command):
        if isinstance(command,DeleteSelectionCommand):
            return self.repository.delete_selection(command)
        if isinstance(command, (DeleteEventCommand, CompleteEventCommand)):
            intent = Intent.DELETE_EVENT if isinstance(command, DeleteEventCommand) else Intent.COMPLETE_EVENT
            return self.repository.change_status(command, intent)
        if isinstance(command, (CreateEventCommand, BatchCreateEventCommand, CreateSeriesCommand)):
            return self.create.execute(command)
        if isinstance(command, UpdateSeriesCommand):
            return self.repository.update_batch(command.updates)
        if isinstance(command, UpdateEventCommand):
            return [self.repository.update(command)]
        if isinstance(command, ListEventsCommand):
            return self.list.execute(command)
        if isinstance(command, SearchEventsCommand):
            return self.search.execute(command)
        raise TypeError("Unsupported business command")
