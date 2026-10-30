from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.commands import EventResponse


class TodayPlanResponse(BaseModel):
    today: date
    timezone: str
    as_of: datetime
    total_today: int = Field(ge=0)
    upcoming: list[EventResponse]
    without_time: list[EventResponse]
    overdue_today: list[EventResponse]
    overdue_earlier: list[EventResponse]
    ongoing_ids: list[UUID]
    undated_count: int = Field(ge=0)
