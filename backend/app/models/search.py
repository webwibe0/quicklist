"""Optional vector foundation. Imported by Alembic/tests, not by request handlers."""
from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.search_content import SearchContent
from app.db.session import Base
from app.models.entities import Event


class EventSearchDocument(Base):
    __tablename__ = "event_search_documents"
    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "event_id"], ["events.owner_id", "events.id"],
                             name="fk_document_owned_event", ondelete="CASCADE"),
        CheckConstraint("source_title ~ '[^[:space:]]'", name="ck_search_document_title"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_search_document_hash"),
        CheckConstraint("text_version > 0", name="ck_search_document_version"),
        Index("ix_search_document_content", "content_hash", "text_version"),
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_title: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    text_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    @classmethod
    def current_event_condition(cls):
        """Mandatory eligibility predicate for future reads; never use stale title snapshots."""
        return (cls.event_id == Event.id) & (cls.owner_id == Event.owner_id) & (cls.source_title == Event.title) & (Event.status != "deleted")


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"
    __table_args__ = (
        UniqueConstraint("content_hash", "model", "dimensions", "text_version",
                         name="uq_embedding_cache_profile_content"),
        CheckConstraint("canonical_text ~ '[^[:space:]]'", name="ck_embedding_cache_text"),
        CheckConstraint("content_hash = encode(sha256(convert_to(canonical_text, 'UTF8')), 'hex')",
                        name="ck_embedding_cache_hash"),
        CheckConstraint("length(trim(model)) > 0", name="ck_embedding_cache_model"),
        CheckConstraint("dimensions = 1536", name="ck_embedding_cache_dimensions"),
        CheckConstraint("text_version > 0", name="ck_embedding_cache_version"),
        CheckConstraint("status IN ('pending','processing','ready','error')", name="ck_embedding_cache_status"),
        CheckConstraint("(status = 'ready') = (embedding IS NOT NULL)", name="ck_embedding_cache_ready"),
        CheckConstraint("embedding IS NULL OR vector_norm(embedding) > 0", name="ck_embedding_cache_nonzero"),
        CheckConstraint("attempts >= 0", name="ck_embedding_cache_attempts"),
        CheckConstraint("(lease_until IS NULL) = (lease_token IS NULL) AND "
                        "(status = 'processing') = (lease_until IS NOT NULL)", name="ck_embedding_cache_lease"),
        CheckConstraint("last_error_code IS NULL OR last_error_code ~ '^[A-Z0-9_]{1,64}$'",
                        name="ck_embedding_cache_error_code"),
        Index("ix_embedding_cache_retry", "status", "next_retry_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    content_hash: Mapped[str] = mapped_column(Text)
    canonical_text: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int] = mapped_column(Integer, server_default="1536")
    text_version: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(1536))
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column()
    last_error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    def assert_content(self, content: SearchContent):
        """A cache hit MUST verify canonical text, not trust the hash alone."""
        if (self.content_hash, self.text_version, self.canonical_text) != (
            content.content_hash, content.text_version, content.canonical_text,
        ):
            raise ValueError("Embedding cache canonical content mismatch")
