"""An NLU context bound must not delete persisted user-visible conversation history."""

from uuid import UUID, uuid4

from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.schemas.commands import EventActionRequest, MessageRequest
from app.services.authentication import Identity
from app.services.dialogue import DialogueService
from app.services.message_transaction import execute_message
from app.services.responses import ResponseBuilder
from app.temporal.resolver import TemporalResolver
from app.use_cases.actions import ActionRouter
from tests.fakes import FakeLLMProvider
from tests.test_context_updates import sqlite_jsonb_for_context_test  # noqa: F401


def test_message_after_fifty_turns_retains_the_first_message_and_all_previous_entries():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Event.__table__.create(engine)
    Conversation.__table__.create(engine)
    original = [
        {"role": "user" if i % 2 == 0 else "assistant", "text": f"Synthetic history {i}"}
        for i in range(102)
    ]
    cid = uuid4()
    with Session(engine) as session, session.begin():
        session.add(Conversation(id=cid, messages=original))

    def factory(session, provider):
        return DialogueService(
            provider,
            TemporalResolver("Europe/Moscow"),
            ActionRouter(EventRepository(session, 50)),
            ResponseBuilder(50),
        )

    with Session(engine) as session:
        execute_message(
            session,
            FakeLLMProvider({}),
            factory,
            MessageRequest(conversation_id=cid, message="Synthetic unknown"),
        )
    with Session(engine) as check:
        saved = check.get(Conversation, cid)
        assert saved.messages[:102] == original
        assert len(saved.messages) == 104
    engine.dispose()


def test_card_action_after_fifty_turns_also_retains_every_previous_entry():
    from app.api.routes import event_action

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Event.__table__.create(engine)
    Conversation.__table__.create(engine)
    original = [{"role": "user", "text": f"Synthetic history {i}"} for i in range(102)]
    cid, eid = uuid4(), uuid4()
    with Session(engine) as session, session.begin():
        session.add_all([Conversation(id=cid, messages=original),
                         Event(id=eid, type="task", title="Synthetic history task")])
    owner = Identity(UUID("20000000-0000-4000-8000-000000000001"), "legacy-test@example.test")
    with Session(engine) as session:
        result = event_action(eid, EventActionRequest(action="COMPLETE_EVENT", conversation_id=cid),
                              BackgroundTasks(), session, FakeLLMProvider({}), owner)
        assert result.action == "COMPLETE_EVENT" and not result.requires_input
    with Session(engine) as check:
        saved = check.get(Conversation, cid)
        assert saved.messages[:102] == original
        assert len(saved.messages) == 104
    engine.dispose()
