from datetime import date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.models.accounts import User  # noqa: F401


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("owner_id", "source", "source_id", name="uq_events_source_id"),
        UniqueConstraint("owner_id", "id", name="uq_events_owner_id"),
        CheckConstraint("length(trim(title)) > 0", name="ck_events_title"),
        CheckConstraint(
            "type = 'task' OR (type = 'meeting' AND event_date IS NOT NULL) OR "
            "(type = 'reminder' AND event_date IS NOT NULL AND event_time IS NOT NULL)",
            name="ck_events_required_schedule",
        ),
        Index("ix_events_date", "event_date"),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    type: Mapped[str] = mapped_column(Enum("task", "reminder", "meeting", name="event_type"))
    title: Mapped[str] = mapped_column(Text)
    event_date: Mapped[date | None] = mapped_column(Date)
    event_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    status: Mapped[str] = mapped_column(
        Enum("pending", "completed", "cancelled", "deleted", name="event_status"),
        default="pending", server_default="pending",
    )
    raw_user_text: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        Enum("user", "dataset", name="event_source"), default="user", server_default="user",
    )
    source_id: Mapped[str | None] = mapped_column(Text)
    recurrence_rule: Mapped[str | None] = mapped_column(Text)
    recurrence_series_id: Mapped[UUID | None] = mapped_column()
    recurrence_until: Mapped[date | None] = mapped_column(Date)
    type_inferred: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("owner_id", "id", name="uq_conversations_owner_id"),
        ForeignKeyConstraint(["owner_id", "last_event_id"], ["events.owner_id", "events.id"],
                             name="fk_conversation_owned_last_event"),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    last_event_id: Mapped[UUID | None] = mapped_column()
    messages: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    context: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    pending_action: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
