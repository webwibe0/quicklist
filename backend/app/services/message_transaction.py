"""Keep dialogue writes atomic without holding locks across an external NLU call."""
from copy import deepcopy

from app.ai.embeddings import EmbeddingError, OpenAIEmbeddingProvider, validate_batch
from app.ai.provider import ProviderError
from app.repositories.conversations import ConversationRepository
from app.services.hybrid_search import EmbeddingRequired


class ParseRequired(Exception):
    """Unwind the preparation transaction before invoking the external provider."""

    def __init__(self, text, context):
        self.text = text
        self.context = deepcopy(context)


class PreparedProvider:
    def __init__(self):
        self.request = None
        self.command = None
        self.embedding_request = None
        self.embedding_vector = None
        self.embedding_test_provider = None

    def query_embedding(self, query, settings):
        if self.embedding_request is None:
            raise EmbeddingRequired(query, settings)
        if query != self.embedding_request:
            raise ProviderError("Диалог изменился во время обработки. Повторите запрос.")
        return self.embedding_vector

    def parse_command(self, text, context):
        if self.request is None:
            raise ParseRequired(text, context)
        if text != self.request.text or context != self.request.context:
            # A concurrent request changed the dialogue while NLU was running.
            # Never apply an answer grounded in a stale PendingAction/context.
            raise ProviderError("Диалог изменился во время обработки. Повторите запрос.")
        return self.command.model_copy(deep=True)


def execute_message(session, provider, service_factory, request, *, embedding_provider=None):
    prepared = PreparedProvider()
    prepared.embedding_test_provider = embedding_provider
    original_state = None
    initialized = False

    def apply():
        nonlocal original_state, initialized
        with session.begin():
            conversation = ConversationRepository(session).get_or_create(request.conversation_id)
            state = deepcopy((conversation.pending_action, conversation.context, conversation.last_event_id))
            if not initialized:
                original_state = state
                initialized = True
            elif state != original_state:
                # Check before handle(): changed pending state could otherwise consume
                # the text as a deterministic confirmation without asking the parser.
                raise ProviderError("Диалог изменился во время обработки. Повторите запрос.")
            result = service_factory(session, prepared).handle(conversation, request.message)
            conversation.messages = [*(conversation.messages or []),
                {"role": "user", "text": request.message},
                {"role": "assistant", "text": result.message, "response": result.model_dump(mode="json")}]
        # Commit has succeeded here. PendingAction and event mutations share this boundary.
        return result

    # At most one NLU call and one query embedding, both outside DB transactions.
    for _ in range(3):
        try:
            return apply()
        except ParseRequired as required:
            if prepared.request is not None:
                raise ProviderError("Повторите запрос.") from None
            prepared.request = required
            prepared.command = provider.parse_command(required.text, deepcopy(required.context))
        except EmbeddingRequired as required:
            if prepared.embedding_request is not None:
                raise ProviderError("Повторите запрос.") from None
            prepared.embedding_request = required.query
            owned = embedding_provider is None
            client = embedding_provider
            try:
                if client is None:
                    client = OpenAIEmbeddingProvider(required.settings, allow_paid_api=True)
                prepared.embedding_vector = validate_batch(
                    client.embed([required.query]), 1, required.settings.embedding_dimensions,
                )[0]
            except EmbeddingError:
                prepared.embedding_vector = None  # Deterministic FTS fallback, no hidden retries.
            finally:
                if owned and client is not None:
                    client.close()
    raise ProviderError("Повторите запрос.")
