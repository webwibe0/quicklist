"""Owner-scoped chat history; no shared browser transcript cache."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.ownership import bind_owner
from app.db.session import get_session
from app.models.entities import Conversation
from app.services.authentication import Identity, require_user

router = APIRouter(prefix="/api/v1/conversations", tags=["history"])


@router.get("")
def conversations(session: Session = Depends(get_session), user: Identity = Depends(require_user)):
    bind_owner(session, user.id)
    return [
        {"id": str(row.id), "updated_at": row.updated_at}
        for row in session.scalars(
            select(Conversation).order_by(Conversation.updated_at.desc(), Conversation.id).limit(50)
        )
    ]


@router.get("/{conversation_id}")
def history(
    conversation_id: UUID,
    session: Session = Depends(get_session),
    user: Identity = Depends(require_user),
):
    bind_owner(session, user.id)
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "Диалог не найден.")
    return {"conversation_id": str(conversation.id), "entries": conversation.messages or []}


class LegacyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    text: str = Field(max_length=64000)


class LegacyHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[LegacyEntry] = Field(max_length=100)


@router.post("/{conversation_id}/legacy-history")
def import_legacy(
    conversation_id: UUID,
    body: LegacyHistory,
    session: Session = Depends(get_session),
    user: Identity = Depends(require_user),
):
    if not user.is_legacy_owner:
        raise HTTPException(403, "Импорт старой истории доступен только первоначальному владельцу.")
    bind_owner(session, user.id)
    with session.begin():
        conversation = session.scalar(
            select(Conversation).where(Conversation.id == conversation_id).with_for_update()
        )
        if conversation is None:
            raise HTTPException(404, "Диалог не найден.")
        if not conversation.messages:
            # Plain history only: imported cards/UUIDs never become trusted business context.
            conversation.messages = [item.model_dump() for item in body.entries]
        return {"conversation_id": str(conversation.id), "entries": conversation.messages or []}
