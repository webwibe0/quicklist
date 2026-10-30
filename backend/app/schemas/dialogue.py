import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.commands import (
    BatchCreateEventCommand,
    CreateEventCommand,
    CreateSeriesCommand,
    DeleteEventCommand,
    DeleteSelectionCommand,
    EventResponse,
    EventType,
    Intent,
    ParsedCommand,
    UpdateEventCommand,
    UpdateSeriesCommand,
)
from app.temporal.resolver import (
    DAYPARTS,
    ELAPSED_PATTERN,
    TemporalAmbiguity,
    TemporalResolver,
    TemporalResult,
    TimeFragments,
    clean,
)


class ActionDraft(BaseModel):
    """Collected entities and temporal fragments, independent of the latest NLU patch."""
    command: ParsedCommand
    temporal: TemporalResult
    raw_user_text: str

    def time_context(self, resolver: TemporalResolver) -> TimeFragments:
        fragments = self.temporal.time_fragments
        if fragments.hour is not None or fragments.daypart or fragments.flexible:
            return fragments
        # Existing persisted pending actions predate typed fragments.
        try:
            return resolver.time_fragments(self.command.time_expression or "")
        except TemporalAmbiguity:
            return fragments

    def merge(self, patch: ParsedCommand, temporal: TemporalResult, text: str,
              resolver: TemporalResolver, *, single_time: bool):
        values = patch.model_dump(exclude_none=True)
        replaced = {}
        if "time_expressions" in values:
            replaced = {"time_expression": None, "end_time_expression": None}
        elif "time_expression" in values:
            replaced["time_expressions"] = None
        if patch.intent == Intent.UPDATE_EVENT:
            if "shift_expression" in values:
                replaced.update(date_expression=None, time_expression=None, end_time_expression=None)
            elif any(field in values for field in (
                "date_expression", "time_expression", "end_time_expression",
            )):
                replaced["shift_expression"] = None
                if "date_expression" in values:
                    replaced["time_expression"] = None
        merged = self.command.model_dump() | replaced | values
        if single_time and "time_expression" in values and "end_time_expression" not in values:
            merged["end_time_expression"] = None
        self.command = ParsedCommand.model_validate(merged)
        saved = self.temporal.model_copy(deep=True)
        relative_patch = any(re.fullmatch(ELAPSED_PATTERN, clean(value)) for value in (
            patch.date_expression or '', patch.time_expression or '',
        ))
        saved.issues.pop("referent", None)
        if "recurrence_expression" in values:
            saved.issues.pop("recurrence", None)
        groups = {
            "date_expression": ("date", ("event_date", "event_time")),
            "time_expression": ("time", ("event_time", "event_times", "time_fragments", "time_unspecified")),
            "time_expressions": ("time", ("event_time", "event_times", "time_fragments")),
            "end_time_expression": ("end_time", ("end_time",)),
            "period_expression": ("period", ("period_start", "period_end", "search_direction")),
        }
        for expression, (issue, fields) in groups.items():
            if expression in values:
                saved.issues.pop(issue, None)
                if issue in temporal.issues:
                    saved.issues[issue] = temporal.issues[issue]
                for field in fields:
                    if field == "event_time" and expression == "date_expression" and temporal.event_time is None:
                        continue
                    setattr(saved, field, getattr(temporal, field))
        if relative_patch and temporal.event_date and temporal.event_time:
            if (patch.date_expression is None and self.temporal.event_date
                    and self.temporal.event_date != temporal.event_date):
                saved.issues['date'] = 'Указанная ранее дата и интервал от текущего момента расходятся. Уточните дату.'
            else:
                saved.event_date = temporal.event_date
                saved.event_time = temporal.event_time
                saved.issues.pop('time', None)
        if "time_expression" in values and temporal.interval_requested:
            saved.end_time = temporal.end_time
            saved.interval_start_expression = temporal.interval_start_expression
            saved.interval_end_expression = temporal.interval_end_expression
            saved.overnight = temporal.overnight
            saved.issues.pop("end_time", None)
            if "end_time" in temporal.issues:
                saved.issues["end_time"] = temporal.issues["end_time"]
        if (saved.interval_start_expression and saved.interval_end_expression
                and clean(patch.time_expression or "") in DAYPARTS
                and "time_expression" in values):
            interval = resolver.resolve_interval(saved.interval_start_expression, saved.interval_end_expression,
                                                 daypart=temporal.time_fragments.daypart)
            saved.event_time, saved.end_time, saved.overnight = interval.event_time, interval.end_time, interval.overnight
            saved.issues.pop("time", None)
            saved.issues.pop("end_time", None)
            saved.issues.update(interval.issues)
        saved.interval_requested |= temporal.interval_requested
        resolver.validate_interval(saved)
        if saved.event_date and saved.event_time and not saved.issues:
            try:
                for clock in saved.event_times or [saved.event_time]:
                    resolver.validate_local_datetime(saved.event_date, clock)
            except TemporalAmbiguity as exc:
                saved.issues["time"] = str(exc)
        self.temporal = saved
        self.raw_user_text = (self.raw_user_text + "\n" + text)[-16000:]

    def as_task(self, text: str) -> "ActionDraft":
        temporal = self.temporal.model_copy(deep=True)
        temporal.event_time = temporal.end_time = None
        temporal.event_times = []
        temporal.time_fragments = TimeFragments()
        temporal.interval_requested = False
        temporal.interval_start_expression = temporal.interval_end_expression = None
        temporal.overnight = False
        temporal.issues.pop("time", None)
        temporal.issues.pop("end_time", None)
        return ActionDraft(
            command=self.command.model_copy(update={
                "event_type": EventType.TASK, "time_expression": None,
                "time_expressions": None, "end_time_expression": None,
            }), temporal=temporal, raw_user_text=(self.raw_user_text + "\n" + text)[-16000:],
        )


class PendingAction(ActionDraft):
    missing_fields: list[str]
    actions: list[ActionDraft] | None = None
    action_index: int | None = None
    task_conversion_requested: bool = False
    # Frozen business command, populated only after target and time validation.
    confirmation: UpdateEventCommand | UpdateSeriesCommand | CreateEventCommand | CreateSeriesCommand | BatchCreateEventCommand | DeleteEventCommand | DeleteSelectionCommand | None = None
    confirmation_kind: Literal[
    "past",
    "duplicate",
    "conflict",
    "batch",
    "task_conversion",
    "delete",
    "unscheduled_meeting",
] | None = None
    confirmation_event: EventResponse | None = None
    candidate_ids: list[UUID] | None = None
    candidate_events: list[EventResponse] | None = None
    named_selected: list[EventResponse] | None = None
    target_event_id: UUID | None = None
    alternative: CreateEventCommand | CreateSeriesCommand | BatchCreateEventCommand | None = None
    approved: list[str] = Field(default_factory=list)


class DialogueContext(BaseModel):
    pending_action: PendingAction | None = None
    last_created_event: EventResponse | None = None
    last_listed_events: list[EventResponse] = Field(default_factory=list)
    last_event_scope: Literal["last_created", "last_listed"] | None = None
