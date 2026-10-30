from datetime import date, datetime, time
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.temporal.recurrence import MAX_RECURRENCE_OCCURRENCES


class Intent(StrEnum):
    CREATE_EVENT = "CREATE_EVENT"
    UPDATE_EVENT = "UPDATE_EVENT"
    DELETE_EVENT = "DELETE_EVENT"
    COMPLETE_EVENT = "COMPLETE_EVENT"
    LIST_EVENTS = "LIST_EVENTS"
    SEARCH_EVENTS = "SEARCH_EVENTS"
    UNKNOWN = "UNKNOWN"


class EventType(StrEnum):
    TASK = "TASK"
    REMINDER = "REMINDER"
    MEETING = "MEETING"


class ParsedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    event_type: EventType | None = None
    title: str | None = Field(default=None, max_length=500)
    date_expression: str | None = Field(default=None, max_length=200)
    time_expression: str | None = Field(default=None, max_length=200)
    time_expressions: list[str] | None = Field(default=None, min_length=1, max_length=12)
    end_time_expression: str | None = Field(default=None, max_length=200)
    period_expression: str | None = Field(default=None, max_length=200)
    recurrence_expression: str | None = Field(default=None, max_length=200)
    recurrence_scope: Literal["occurrence", "series"] | None = None
    new_title: str | None = Field(default=None, min_length=1, max_length=500)
    search_query: str | None = Field(default=None, max_length=500)

    delete_targets: list[str] | None = Field(default=None, min_length=2, max_length=12)
    actions: list["ParsedCommand"] | None = Field(default=None, min_length=2, max_length=12)
    relative_anchor_expression: str | None = Field(default=None, max_length=100)

    # References describe a scope, never a model-selected database ID.
    event_reference: Literal["last_created", "last_listed"] | None = None
    event_index: int | None = Field(default=None, ge=1, le=1000)
    shift_expression: str | None = Field(default=None, max_length=200)
    recipient_expression: str | None = Field(default=None,max_length=200)


class UpdateEventCommand(BaseModel):
    event_id: UUID
    event_date: date | None
    event_time: time | None
    end_time: time | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)


class UpdateSeriesCommand(BaseModel):
    recurrence_series_id: UUID
    updates: list[UpdateEventCommand] = Field(min_length=1, max_length=MAX_RECURRENCE_OCCURRENCES)


class CreateEventCommand(BaseModel):
    event_type: EventType
    title: str = Field(min_length=1, max_length=500)
    event_date: date | None
    event_time: time | None
    raw_user_text: str
    end_time: time | None = None
    recurrence_expression: str | None = None
    recurrence_rule: str | None = None
    recurrence_series_id: UUID | None = None
    recurrence_until: date | None = None


class DeleteEventCommand(BaseModel):
    event_id: UUID
    recurrence_series_id: UUID | None = None


class DeleteSelectionCommand(BaseModel):
    event_ids: list[UUID] = Field(min_length=1,max_length=200)


class CompleteEventCommand(BaseModel):
    event_id: UUID


class BatchCreateEventCommand(BaseModel):
    events: list[CreateEventCommand] = Field(min_length=2, max_length=12)


class CreateSeriesCommand(BaseModel):
    # Already expanded and frozen, with no further date calculation during save.
    occurrences: list[CreateEventCommand] = Field(min_length=1, max_length=MAX_RECURRENCE_OCCURRENCES)


def creation_commands(command):
    if isinstance(command, CreateSeriesCommand):
        return command.occurrences
    return command.events if isinstance(command, BatchCreateEventCommand) else [command]


class ListEventsCommand(BaseModel):
    include_unscheduled: bool = False
    status: Literal["pending", "completed", "cancelled"] | None = None
    recurrence_required: bool | None = None
    include_inactive: bool = False
    start: date
    end: date
    event_type: EventType | None = None
    temporal_direction: Literal["past", "future"] | None = None
    as_of: datetime | None = None


class SearchEventsCommand(BaseModel):
    status: Literal["pending", "completed", "cancelled"] | None = None
    recurrence_required: bool | None = None
    raw_query: str | None = None
    query: str = Field(min_length=1, max_length=500)
    start: date | None = None
    end: date | None = None
    event_type: EventType | None = None
    temporal_direction: Literal["past", "future"] | None = None
    as_of: datetime | None = None


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID | None = None
    message: str = Field(min_length=1, max_length=4000)


class EventActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID | None = None
    action: Literal["DELETE_EVENT", "COMPLETE_EVENT"]


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    type: str
    title: str
    event_date: date | None
    event_time: time | None
    end_time: time | None
    status: str
    source: str
    type_inferred: bool
    recurrence_rule: str | None = None
    recurrence_series_id: UUID | None = None
    recurrence_until: date | None = None


class MessageResponse(BaseModel):
    conversation_id: UUID
    message: str
    action: Intent
    events: list[EventResponse] = Field(default_factory=list)
    requires_input: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    search_mode: Literal["hybrid", "lexical", "lexical_fallback"] | None = None
    unverified_count: int = 0
