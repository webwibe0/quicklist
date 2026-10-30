"""Explicit, bounded background execution; never registered with FastAPI startup."""

from threading import Event
from uuid import UUID

from app.ai.embeddings import (
    EmbeddingError,
    EmbeddingProfile,
    OpenAIEmbeddingProvider,
    validate_batch,
)
from app.repositories.indexing import IndexingRepository


class IndexingWorker:
    def __init__(self, engine, provider, settings, *, clock=None):
        self.engine, self.provider, self.settings, self.clock = engine, provider, settings, clock
        self.profile = EmbeddingProfile(settings.embedding_model, settings.embedding_dimensions)
        if (settings.embedding_production_indexing_approved
                and not isinstance(provider, OpenAIEmbeddingProvider)):
            raise ValueError("Primary indexing requires the approved real embedding provider")
        if provider.profile != self.profile:
            raise ValueError("Worker/provider embedding profiles must match")
        if settings.embedding_lease_seconds <= 4 * settings.embedding_timeout_seconds:
            raise ValueError("Lease must exceed four provider timeout phases")

    def run_once(self, *, after=None, stop=None, changes_only=False):
        stats = dict(
            disabled=not self.settings.embedding_worker_enabled,
            provider_calls=0,
            inputs=0,
            input_bytes=0,
            estimated_tokens_approx=0,
            completed=0,
            retried=0,
            failed=0,
            stale_claims=0,
            recovered_leases=0,
            removed_stale=0,
            next_cursor=None,
        )
        stop = stop or Event()
        if stats["disabled"] or stop.is_set():
            return stats
        repository = IndexingRepository(self.engine, self.profile, self.settings, clock=self.clock)
        if changes_only:
            stats.update(repository.reconcile(self.settings.embedding_max_documents, changes_only=True))
        else:
            stats.update(repository.reconcile(self.settings.embedding_max_documents, after=after))
        remaining = self.settings.embedding_max_documents
        byte_budget = self.settings.embedding_max_run_bytes
        while remaining > 0 and byte_budget > 0 and not stop.is_set():
            claims, size, recovered = repository.claim(
                min(remaining, self.settings.embedding_batch_size), byte_budget
            )
            stats["recovered_leases"] += recovered
            if not claims:
                break
            stats["inputs"] += len(claims)
            stats["input_bytes"] += size
            stats["estimated_tokens_approx"] += (
                size + 3
            ) // 4  # Heuristic, NOT a hard token budget.
            remaining -= len(claims)
            byte_budget -= size
            error, vectors = None, None
            # claim() has COMMITTED and closed its Session before any provider code is invoked.
            try:
                stats["provider_calls"] += 1
                vectors = self.provider.embed([claim.content.canonical_text for claim in claims])
                vectors = validate_batch(vectors, len(claims), self.profile.dimensions)
            except EmbeddingError as failure:
                error = failure
            except Exception:
                error = EmbeddingError("PROVIDER_ERROR")
            if error and error.code == 'BUDGET_BLOCKED':
                repository.defer_unpaid(claims)
                stats['budget_blocked'] = True
                break
            finished = repository.finish(claims, vectors=vectors, error=error)
            for key, value in finished.items():
                stats[key] += value
        stats["removed_stale"] += repository.remove_stale_documents(
            self.settings.embedding_max_documents
        )
        return stats

    def run_periodic(self, stop, *, cycles):
        if cycles < 1:
            raise ValueError("Periodic execution must have an explicit positive cycle limit")
        after = None
        for index in range(cycles):
            if stop.is_set():
                break
            stats = self.run_once(after=after, stop=stop)
            yield stats
            after = UUID(stats["next_cursor"]) if stats["next_cursor"] else None
            if index + 1 < cycles and stop.wait(self.settings.embedding_poll_seconds):
                break
