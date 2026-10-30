"""Short PostgreSQL transactions only. Provider I/O does not belong in this module."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker

from app.ai.embeddings import EmbeddingProfile, validate_batch, validate_vector
from app.core.search_content import SearchContent, search_content
from app.models.entities import Event
from app.models.search import EmbeddingCache, EventSearchDocument
from app.services.indexing_target import verify_worker_target


@dataclass(frozen=True)
class EmbeddingClaim:
    id: UUID
    token: UUID
    content: SearchContent
    profile: EmbeddingProfile


def retry_delay(attempts, settings):
    return min(
        settings.embedding_retry_max_seconds,
        settings.embedding_retry_base_seconds * 2 ** max(0, min(attempts - 1, 10)),
    )


class IndexingRepository:
    def __init__(self, engine, profile, settings, *, clock=None):
        verify_worker_target(engine, settings)
        self.sessions = sessionmaker(engine, expire_on_commit=False, info={"system_access": True})
        self.profile, self.settings, self.clock = profile, settings, clock

    def now(self, session):
        return self.clock() if self.clock else session.scalar(select(func.clock_timestamp()))

    def profile_filter(self):
        return (
            EmbeddingCache.model == self.profile.model,
            EmbeddingCache.dimensions == self.profile.dimensions,
            EmbeddingCache.text_version == self.profile.text_version,
        )

    def reconcile(self, limit, *, after=None, changes_only=False):
        stats = dict(
            scanned=0,
            staged=0,
            cache_created=0,
            cache_reused=0,
            invalid_content=0,
            next_cursor=None,
        )
        with self.sessions.begin() as session:
            query = (
                select(Event)
                .order_by(Event.id)
                .limit(limit)
                .with_for_update(read=True, of=Event, skip_locked=True)
            )
            if after is not None:
                query = query.where(Event.id > after)
            if changes_only:
                query = query.outerjoin(EventSearchDocument, EventSearchDocument.event_id == Event.id).where(
                    Event.status != 'deleted',
                    or_(EventSearchDocument.event_id.is_(None),
                        EventSearchDocument.source_title != Event.title,
                        EventSearchDocument.text_version != self.profile.text_version),
                )
            events = list(session.scalars(query))
            stats["scanned"] = len(events)
            if len(events) == limit:
                stats["next_cursor"] = str(events[-1].id)
            for event in events:
                document = session.get(EventSearchDocument, event.id)
                if event.status == "deleted":
                    if document:
                        session.delete(document)
                    continue
                try:
                    content = search_content(event.title)
                except ValueError:
                    stats["invalid_content"] += 1
                    if document:
                        session.delete(document)
                    continue
                inserted = session.scalar(
                    insert(EmbeddingCache)
                    .values(
                        id=uuid4(),
                        content_hash=content.content_hash,
                        canonical_text=content.canonical_text,
                        model=self.profile.model,
                        dimensions=self.profile.dimensions,
                        text_version=content.text_version,
                        status="pending",
                        attempts=0,
                    )
                    .on_conflict_do_nothing(constraint="uq_embedding_cache_profile_content")
                    .returning(EmbeddingCache.id)
                )
                cached = session.scalar(
                    select(EmbeddingCache).where(
                        *self.profile_filter(), EmbeddingCache.content_hash == content.content_hash
                    )
                )
                cached.assert_content(content)  # Never trust hash equality alone.
                stats["cache_created" if inserted else "cache_reused"] += 1
                if cached.status == "ready":
                    validate_vector(cached.embedding, self.profile.dimensions)
                if (
                    document is None
                    or document.source_title != event.title
                    or document.content_hash != content.content_hash
                    or document.text_version != content.text_version
                ):
                    # This is a staged snapshot, not a readiness declaration. The existing trigger
                    # checks/locks Event; every future ready lookup must also join live Event + cache.
                    session.execute(
                        insert(EventSearchDocument)
                        .values(
                            owner_id=event.owner_id, event_id=event.id,
                            source_title=event.title,
                            content_hash=content.content_hash,
                            text_version=content.text_version,
                        )
                        .on_conflict_do_update(
                            index_elements=["event_id"],
                            set_=dict(
                                source_title=event.title,
                                content_hash=content.content_hash,
                                text_version=content.text_version,
                                updated_at=func.now(),
                            ),
                        )
                    )
                    stats["staged"] += 1
        return stats

    def _clear_lease(self, row):
        row.lease_token = row.lease_until = None

    def claim(self, limit, byte_budget):
        claims, used_bytes, recovered = [], 0, 0
        with self.sessions.begin() as session:
            now = self.now(session)
            expired = list(
                session.scalars(
                    select(EmbeddingCache)
                    .where(
                        *self.profile_filter(),
                        EmbeddingCache.status == "processing",
                        EmbeddingCache.lease_until <= now,
                    )
                    .order_by(EmbeddingCache.lease_until, EmbeddingCache.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in expired:
                self._clear_lease(row)
                row.status = (
                    "error" if row.attempts >= self.settings.embedding_max_attempts else "pending"
                )
                row.next_retry_at = (
                    now + timedelta(seconds=retry_delay(row.attempts, self.settings))
                    if row.status == "pending"
                    else None
                )
                row.last_error_code, row.updated_at = "LEASE_EXPIRED", now
                recovered += 1
            session.flush()
            eligible = exists(
                select(1)
                .select_from(EventSearchDocument)
                .join(Event, EventSearchDocument.current_event_condition())
                .where(
                    EventSearchDocument.content_hash == EmbeddingCache.content_hash,
                    EventSearchDocument.text_version == EmbeddingCache.text_version,
                )
            )
            rows = list(
                session.scalars(
                    select(EmbeddingCache)
                    .where(
                        *self.profile_filter(),
                        eligible,
                        EmbeddingCache.status == "pending",
                        or_(
                            EmbeddingCache.next_retry_at.is_(None),
                            EmbeddingCache.next_retry_at <= now,
                        ),
                    )
                    .order_by(EmbeddingCache.created_at, EmbeddingCache.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                content = search_content(row.canonical_text)
                row.assert_content(content)
                size = len(content.canonical_text.encode("utf-8"))
                if (
                    size > self.settings.embedding_max_input_bytes
                    or row.attempts >= self.settings.embedding_max_attempts
                ):
                    row.status, row.next_retry_at = "error", None
                    row.last_error_code = (
                        "INPUT_TOO_LONG"
                        if size > self.settings.embedding_max_input_bytes
                        else "ATTEMPTS_EXHAUSTED"
                    )
                    continue
                if used_bytes + size > byte_budget:
                    continue
                row.status, row.lease_token = "processing", uuid4()
                row.lease_until = now + timedelta(seconds=self.settings.embedding_lease_seconds)
                row.attempts += 1
                row.last_error_code, row.updated_at = None, now
                claims.append(EmbeddingClaim(row.id, row.lease_token, content, self.profile))
                used_bytes += size
        return claims, used_bytes, recovered

    def finish(self, claims, *, vectors=None, error=None):
        if error is None:
            vectors = validate_batch(vectors, len(claims), self.profile.dimensions)
        result = dict(completed=0, retried=0, failed=0, stale_claims=0)
        with self.sessions.begin() as session:
            for index, claim in enumerate(claims):
                row = session.scalar(
                    select(EmbeddingCache).where(EmbeddingCache.id == claim.id).with_for_update()
                )
                # A row lock may wait past lease expiry; inspect time only after ownership is locked.
                now = self.now(session)
                if (
                    row is None
                    or row.status != "processing"
                    or row.lease_token != claim.token
                    or row.lease_until <= now
                ):
                    result["stale_claims"] += 1
                    continue
                row.assert_content(claim.content)
                if (
                    claim.profile != self.profile
                    or row.model != self.profile.model
                    or row.dimensions != self.profile.dimensions
                ):
                    raise ValueError("Embedding claim profile mismatch")
                self._clear_lease(row)
                row.updated_at = now
                if error is None:
                    row.embedding, row.status = vectors[index], "ready"
                    row.next_retry_at = row.last_error_code = None
                    result["completed"] += 1
                else:
                    retry = error.retryable and row.attempts < self.settings.embedding_max_attempts
                    row.status, row.last_error_code = ("pending" if retry else "error"), error.code
                    row.next_retry_at = (
                        now + timedelta(seconds=retry_delay(row.attempts, self.settings))
                        if retry
                        else None
                    )
                    result["retried" if retry else "failed"] += 1
        return result

    def remove_stale_documents(self, limit):
        removed = 0
        with self.sessions.begin() as session:
            rows = session.execute(
                select(Event, EventSearchDocument)
                .select_from(Event)
                .join(EventSearchDocument, EventSearchDocument.event_id == Event.id)
                .where(
                    or_(Event.status == "deleted", Event.title != EventSearchDocument.source_title)
                )
                .order_by(Event.id)
                .limit(limit)
                .with_for_update(read=True, of=Event, skip_locked=True)
            ).all()
            for event, document in rows:
                removed += session.execute(
                    delete(EventSearchDocument).where(
                        EventSearchDocument.event_id == event.id,
                        EventSearchDocument.source_title == document.source_title,
                        EventSearchDocument.content_hash == document.content_hash,
                        EventSearchDocument.text_version == document.text_version,
                    )
                ).rowcount
        return removed

    def defer_unpaid(self, claims):
        """Budget/rate refusal is not a provider attempt. Keep durable work pending."""
        with self.sessions.begin() as session:
            for claim in claims:
                row=session.scalar(select(EmbeddingCache).where(EmbeddingCache.id==claim.id).with_for_update())
                if row and row.status=='processing' and row.lease_token==claim.token:
                    self._clear_lease(row)
                    row.status='pending'
                    row.attempts=max(0,row.attempts-1)
                    row.last_error_code='BUDGET_BLOCKED'
                    row.next_retry_at=self.now(session)+timedelta(seconds=self.settings.embedding_poll_seconds)

    def ready_for_event(self, event_id, *, owner_id):
        # An event-specific eligibility helper, NOT a search/query API. Future tenant access
        # checks must constrain Event before calling this; vectors never grant event access.
        with self.sessions() as session:
            row = session.execute(
                select(Event, EventSearchDocument, EmbeddingCache)
                .select_from(Event)
                .join(EventSearchDocument, EventSearchDocument.current_event_condition())
                .join(
                    EmbeddingCache,
                    (EmbeddingCache.content_hash == EventSearchDocument.content_hash)
                    & (EmbeddingCache.text_version == EventSearchDocument.text_version),
                )
                .where(
                    Event.id == event_id, Event.owner_id == owner_id, *self.profile_filter(), EmbeddingCache.status == "ready"
                )
            ).first()
            if row is None:
                return None
            event, document, cached = row
            content = search_content(event.title)
            if (document.content_hash, document.text_version) != (
                content.content_hash,
                content.text_version,
            ):
                return None
            cached.assert_content(content)
            return validate_vector(cached.embedding, self.profile.dimensions)
