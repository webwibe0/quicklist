from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Conversation


class ConversationNotFound(LookupError):
    pass


class ConversationRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_or_create(self, conversation_id: UUID | None) -> Conversation:
        if conversation_id:
            conversation = self.session.scalar(select(Conversation).where(
                Conversation.id == conversation_id,
            ).with_for_update())
            if conversation is None:
                raise ConversationNotFound("Диалог не найден. Начните новый диалог.")
            return conversation
        conversation = Conversation()
        self.session.add(conversation)
        self.session.flush()
        return conversation
