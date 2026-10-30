"""Owner-bound OAuth connection and durable per-event desired state; credentials encrypted."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class GoogleConnection(Base):
    __tablename__ = "google_connections"
    __table_args__ = (UniqueConstraint("owner_id", "id", name="uq_google_connection_owner"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), unique=True)
    generation: Mapped[UUID] = mapped_column(default=uuid4)
    google_subject: Mapped[str | None] = mapped_column(Text)
    account_email: Mapped[str | None] = mapped_column(Text)
    credentials_cipher: Mapped[str | None] = mapped_column(Text)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    error_code: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GoogleOAuthState(Base):
    __tablename__ = "google_oauth_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "connection_id"],
            ["google_connections.owner_id", "google_connections.id"],
            ondelete="CASCADE",
        ),
    )
    state_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    connection_id: Mapped[UUID] = mapped_column()
    generation: Mapped[UUID] = mapped_column()
    session_hash: Mapped[str] = mapped_column(
        ForeignKey("auth_sessions.token_hash", ondelete="CASCADE")
    )
    verifier_cipher: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GoogleEventLink(Base):
    __tablename__ = "google_event_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "event_id"], ["events.owner_id", "events.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["owner_id", "connection_id"], ["google_connections.owner_id", "google_connections.id"]
        ),
        UniqueConstraint("owner_id", "event_id", "google_subject", name="uq_google_event_subject"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    event_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column()
    generation: Mapped[UUID] = mapped_column()
    google_subject: Mapped[str] = mapped_column(Text)
    calendar_id: Mapped[str] = mapped_column(Text, default="primary")
    remote_event_id: Mapped[str] = mapped_column(Text)
    desired: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    synced_revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(Text, default="pending")
    error_code: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
