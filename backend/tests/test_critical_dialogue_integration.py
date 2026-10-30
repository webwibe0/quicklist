"""Reported three-turn clarification persists across independent PostgreSQL sessions."""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.services.dialogue import DialogueService
from app.services.responses import ResponseBuilder
from app.temporal.resolver import TemporalResolver
from app.use_cases.actions import ActionRouter
from tests.fakes import FakeLLMProvider
from tests.test_critical_dialogue import occurrences
from tests.test_user_regressions import NOW

pytestmark = pytest.mark.integration


def test_pg_three_turn_target_clarification_survives_reload_and_commits_one_occurrence(
    db, integration_engine
):
    rows = occurrences(db)
    expected = rows[2].id
    ids = [row.id for row in rows]
    conversation = Conversation(id=uuid4())
    db.add(conversation)
    db.commit()
    cid = conversation.id
    first = "замени постригать ногти на стричь волосы"
    provider = FakeLLMProvider(
        {
            first: {
                "intent": "UPDATE_EVENT",
                "title": "постригать ногти",
                "new_title": "стричь волосы",
            },
            "постригать ногти": {"intent": "UPDATE_EVENT", "title": "постригать ногти"},
        }
    )

    def turn(text):
        with Session(integration_engine, expire_on_commit=False) as session, session.begin():
            conv = session.get(Conversation, cid)
            service = DialogueService(
                provider,
                TemporalResolver("Europe/Moscow"),
                ActionRouter(EventRepository(session, 50)),
                ResponseBuilder(50),
            )
            return service.handle(conv, text, NOW)

    assert len(turn(first).events) == 4
    again = turn("постригать ногти")
    assert again.requires_input and not again.events
    final = turn("20.11.2026")
    assert not final.requires_input and [e.id for e in final.events] == [expected]
    with Session(integration_engine) as check:
        saved = check.scalars(
            select(Event).where(Event.id.in_(ids)).order_by(Event.event_date)
        ).all()
        assert [r.title for r in saved] == [
            "постригать ногти",
            "постригать ногти",
            "стричь волосы",
            "постригать ногти",
        ]
        assert [r.event_date for r in saved] == [date(2026, 11, d) for d in (6, 13, 20, 27)]
        assert check.get(Conversation, cid).pending_action is None
