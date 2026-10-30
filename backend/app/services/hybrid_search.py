"""One-owner database search. Only VERIFIED constraints enter the existing UI."""
import logging
from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError

from app.ai.embeddings import EmbeddingError, EmbeddingProfile, validate_vector
from app.services.search_guard import constraint_assessment
from app.services.search_rank import rrf

logger = logging.getLogger(__name__)


class EmbeddingRequired(Exception):
    def __init__(self, query, settings):
        self.query, self.settings = query, settings


@dataclass
class SearchResult:
    mode: str = 'lexical'
    unverified_count: int = 0
    states: dict = field(default_factory=dict)


def occurrence_group(event):
    # Real series only, partitioned by status AND title after occurrence-only edits.
    if event.recurrence_rule and event.recurrence_series_id:
        return f'series:{event.recurrence_series_id}:{event.status}:{event.title}'
    return str(event.id)


class HybridSearch:
    def __init__(self, repository, settings, prepared):
        self.repository, self.settings, self.prepared = repository, settings, prepared
        self.result = SearchResult()

    def execute(self, command):
        if not self.settings.semantic_search_enabled:
            return self.repository.search(command)
        lexical = self.repository.search(command, candidate_limit=self.settings.semantic_candidate_limit)
        self.result.mode = 'lexical_fallback'
        semantic = []
        profile = EmbeddingProfile(self.settings.embedding_model, self.settings.embedding_dimensions)
        selected_profile = profile == EmbeddingProfile("text-embedding-3-large", 1536)
        if selected_profile and (self.settings.embedding_real_api_enabled
                                 or getattr(self.prepared, "embedding_test_provider", None)):
            vector = self.prepared.query_embedding(command.query, self.settings)
            if vector is not None:
                try:
                    vector = validate_vector(vector, profile.dimensions)
                    # A failed vector SQL statement must not poison the dialogue transaction.
                    with self.repository.session.begin_nested():
                        semantic = self.repository.semantic(command, vector, profile, self.settings)
                    self.result.mode = 'hybrid'
                except (SQLAlchemyError, EmbeddingError, ValueError):
                    logger.warning('Semantic retrieval unavailable; using lexical fallback')
        candidates = {str(event.id): event for event in [*lexical, *semantic]}
        accepted = set()
        query = command.raw_query or command.query
        for key, event in candidates.items():
            assessment = constraint_assessment(query, {'title': event.title, 'status': event.status})
            state = {'PASS': 'VERIFIED', 'CONTRADICTION': 'REJECTED', 'UNVERIFIED': 'UNVERIFIED'}[
                assessment['state']]
            self.result.states[key] = state
            if state == 'VERIFIED':
                accepted.add(key)
            elif state == 'UNVERIFIED':
                self.result.unverified_count += 1
        groups = {key: occurrence_group(event) for key, event in candidates.items()}
        ranking = rrf(
            [str(e.id) for e in lexical if str(e.id) in accepted],
            [str(e.id) for e in semantic if str(e.id) in accepted], groups,
        )
        return [candidates[key] for key in ranking[:self.repository.limit + 1]]
