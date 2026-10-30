"""Temporal/entity drafts persist in PostgreSQL JSONB across independent API requests."""
from datetime import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models.entities import Conversation, Event
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider
from tests.test_integration import client_for
from tests.test_manual_ux_regressions import NOW, TOMORROW, reminder

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("scenario", ["hour-daypart", "daypart-hour", "task-switch", "replace-referent"])
def test_collected_draft_survives_new_session(db, integration_engine, monkeypatch, scenario):
    monkeypatch.setattr(TemporalResolver, "local_now",
                        lambda self, now=None: (now or NOW).astimezone(self.zone))
    first, command, followup, patch, clock = {
        "hour-daypart": ("напомни завтра в 9 позвонить", reminder(time_expression="в 9"),
                         "утра", {"intent": "CREATE_EVENT", "time_expression": "утра"}, time(9)),
        "daypart-hour": ("напомни завтра вечером позвонить", reminder(time_expression="вечером"),
                         "в 8", {"intent": "CREATE_EVENT", "time_expression": "в 8"}, time(20)),
        "task-switch": ("напомни завтра позвонить в любое время", reminder(time_expression="в любое время"),
                        "тогда создай задачу без времени", {}, None),
        "replace-referent": ("напомни завтра в 18:00 позвонить ему", reminder(
            title="Позвонить ему", time_expression="в 18:00",
        ), "напомни завтра в 18:00 позвонить", reminder(event_type=None, time_expression="в 18:00"), time(18)),
    }[scenario]
    try:
        with client_for(integration_engine, FakeLLMProvider({first: command})) as client:
            result = client.post("/api/v1/assistant/messages", json={"message": first})
            assert result.status_code == 200, result.text
            payload = result.json()
            assert payload["requires_input"]
            conversation_id = payload["conversation_id"]
        with Session(integration_engine) as check:
            pending = check.get(Conversation, conversation_id).pending_action
            assert pending["temporal"]["event_date"] == TOMORROW.isoformat()
            if scenario == "hour-daypart":
                assert pending["temporal"]["time_fragments"]["hour"] == 9
            if scenario == "daypart-hour":
                assert pending["temporal"]["time_fragments"]["daypart"] == "evening"
        with client_for(integration_engine, FakeLLMProvider({followup: patch})) as client:
            result = client.post("/api/v1/assistant/messages", json={
                "conversation_id": conversation_id, "message": followup,
            })
            assert result.status_code == 200, result.text
            assert not result.json()["requires_input"]
        with Session(integration_engine) as check:
            event, = check.scalars(select(Event)).all()
            assert event.event_date == TOMORROW and event.event_time == clock
            assert event.title == "Позвонить"
            assert event.type == ("task" if scenario == "task-switch" else "reminder")
            assert check.get(Conversation, conversation_id).pending_action is None
    finally:
        app.dependency_overrides.clear()
