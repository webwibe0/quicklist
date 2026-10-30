"""Real PostgreSQL JSONB/transactions for CREATE confirmations, using a fake LLM."""
from copy import deepcopy
from datetime import time, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import app
from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider
from tests.test_create_business_regressions import BATCH, PAST, SINGLE
from tests.test_integration import client_for
from tests.test_manual_ux_regressions import NOW, TOMORROW, reminder

pytestmark = pytest.mark.integration


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(TemporalResolver, "local_now",
                        lambda self, now=None: (now or NOW).astimezone(self.zone))


@pytest.mark.parametrize("kind,answer,count", [
    ("past", "да", 1), ("past", "нет", 0), ("past", "завтра", 1),
    ("batch", "да", 2), ("batch", "нет", 0),
    ("duplicate", "да", 2), ("duplicate", "отмена", 1),
])
def test_confirmation_roundtrips_postgres_and_survives_new_client(
    db, integration_engine, fixed_clock, kind, answer, count,
):
    text, command = {
        "past": (PAST, reminder(date_expression="сегодня", time_expression="в 9 утра")),
        "batch": (BATCH, reminder(title="Поесть", time_expressions=["в 7 утра", "в 7 вечера"])),
        "duplicate": (SINGLE, reminder(time_expression="в 8 утра")),
    }[kind]
    try:
        with client_for(integration_engine, FakeLLMProvider({text: command})) as client:
            first = client.post("/api/v1/assistant/messages", json={"message": text})
            assert first.status_code == 200, first.text
            payload = first.json()
            conversation_id = payload["conversation_id"]
            if kind == "duplicate":
                payload = client.post("/api/v1/assistant/messages", json={
                    "message": text, "conversation_id": conversation_id,
                }).json()
            assert payload["missing_fields"] == ["confirmation"]
        with Session(integration_engine) as check:
            pending = check.get(Conversation, conversation_id).pending_action
            assert pending["confirmation_kind"] == kind
            assert check.scalar(select(func.count()).select_from(Event)) == (kind == "duplicate")

        class NoLLM:
            def parse_command(self, *args):
                pytest.fail("Persisted confirmation must not call the model")

        with client_for(integration_engine, NoLLM()) as client:
            result = client.post("/api/v1/assistant/messages", json={
                "message": answer, "conversation_id": conversation_id,
            })
            assert result.status_code == 200, result.text
            assert not result.json()["requires_input"]
        with Session(integration_engine) as check:
            events = check.scalars(select(Event)).all()
            assert len(events) == count
            assert check.get(Conversation, conversation_id).pending_action is None
            if kind == "past" and count:
                assert events[0].event_date == (TOMORROW if answer == "завтра" else NOW.date())
                assert events[0].event_time == time(9)
            if kind == "batch" and count:
                assert {event.event_time for event in events} == {time(7), time(19)}
    finally:
        app.dependency_overrides.clear()


def test_api_batch_db_failure_rolls_back_rows_and_keeps_persisted_confirmation(
    db, integration_engine, fixed_clock, monkeypatch,
):
    provider = FakeLLMProvider({BATCH: reminder(
        title="Поесть", time_expressions=["в 7 утра", "в 7 вечера"],
    )})
    try:
        with client_for(integration_engine, provider) as client:
            first = client.post("/api/v1/assistant/messages", json={"message": BATCH}).json()
            conversation_id = first["conversation_id"]
            with Session(integration_engine) as check:
                pending = deepcopy(check.get(Conversation, conversation_id).pending_action)
            original = EventRepository.create
            calls = []

            def fail_second(self, command):
                calls.append(command)
                if len(calls) == 2:
                    command = command.model_copy(update={"title": ""})
                return original(self, command)

            monkeypatch.setattr(EventRepository, "create", fail_second)
            failed = client.post("/api/v1/assistant/messages", json={
                "message": "да", "conversation_id": conversation_id,
            })
            assert failed.status_code == 503
            assert len(calls) == 2
            with Session(integration_engine) as check:
                assert check.scalar(select(func.count()).select_from(Event)) == 0
                assert check.get(Conversation, conversation_id).pending_action == pending
            monkeypatch.setattr(EventRepository, "create", original)
            retried = client.post("/api/v1/assistant/messages", json={
                "message": "да", "conversation_id": conversation_id,
            })
            assert retried.status_code == 200 and len(retried.json()["events"]) == 2
    finally:
        app.dependency_overrides.clear()
def test_daily_recurrence_materializes_one_series(
    db, integration_engine, fixed_clock,
):
    text = "напомни завтра в 9 утра пить воду каждый день 3 раза"
    provider = FakeLLMProvider({
        text: {
            "intent": "CREATE_EVENT",
            "event_type": "REMINDER",
            "title": "Пить воду",
            "date_expression": "завтра",
            "time_expression": "в 9 утра",
            "recurrence_expression": "каждый день 3 раза",
        },
    })

    try:
        with client_for(integration_engine, provider) as client:
            response = client.post(
                "/api/v1/assistant/messages",
                json={"message": text},
            )

            assert response.status_code == 200, response.text
            assert "Создана серия" in response.json()["message"]
            payload = response.json()
            assert not payload["requires_input"]
            assert len(payload["events"]) == 3

        with Session(integration_engine) as check:
            events = check.scalars(
                select(Event).order_by(Event.event_date)
            ).all()

            assert [event.event_date for event in events] == [
                TOMORROW,
                TOMORROW + timedelta(days=1),
                TOMORROW + timedelta(days=2),
            ]
            assert {event.event_time for event in events} == {time(9)}
            assert len({event.recurrence_series_id for event in events}) == 1
            assert events[0].recurrence_series_id is not None
            assert {event.recurrence_rule for event in events} == {
                "FREQ=DAILY;COUNT=3"
            }
    finally:
        app.dependency_overrides.clear()


def test_weekday_recurrence_skips_weekend(
    db, integration_engine, fixed_clock,
):
    text = "напомни завтра в 9 утра проверить почту по будням 4 раза"
    provider = FakeLLMProvider({
        text: {
            "intent": "CREATE_EVENT",
            "event_type": "REMINDER",
            "title": "Проверить почту",
            "date_expression": "завтра",
            "time_expression": "в 9 утра",
            "recurrence_expression": "по будням 4 раза",
        },
    })

    try:
        with client_for(integration_engine, provider) as client:
            response = client.post(
                "/api/v1/assistant/messages",
                json={"message": text},
            )

            assert response.status_code == 200, response.text
            assert "Создана серия" in response.json()["message"]
            assert len(response.json()["events"]) == 4

        with Session(integration_engine) as check:
            events = check.scalars(
                select(Event).order_by(Event.event_date)
            ).all()

            assert [event.event_date.isoformat() for event in events] == [
                "2026-10-16",
                "2026-10-19",
                "2026-10-20",
                "2026-10-21",
            ]
            assert {event.recurrence_rule for event in events} == {
                "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;COUNT=4"
            }
    finally:
        app.dependency_overrides.clear()


def test_monthly_recurrence_skips_missing_calendar_day(
    db, integration_engine, fixed_clock,
):
    text = "напомни 31 октября в 10 утра оплатить счет каждый месяц 31 числа 3 раза"
    provider = FakeLLMProvider({
        text: {
            "intent": "CREATE_EVENT",
            "event_type": "REMINDER",
            "title": "Оплатить счет",
            "date_expression": "31 октября",
            "time_expression": "в 10 утра",
            "recurrence_expression": "каждый месяц 31 числа 3 раза",
        },
    })

    try:
        with client_for(integration_engine, provider) as client:
            response = client.post(
                "/api/v1/assistant/messages",
                json={"message": text},
            )

            assert response.status_code == 200, response.text
            assert "Создана серия" in response.json()["message"]
            assert len(response.json()["events"]) == 3

        with Session(integration_engine) as check:
            events = check.scalars(
                select(Event).order_by(Event.event_date)
            ).all()

            assert [event.event_date.isoformat() for event in events] == [
                "2026-10-31",
                "2026-12-31",
                "2027-01-31",
            ]
            assert {event.recurrence_rule for event in events} == {
                "FREQ=MONTHLY;BYMONTHDAY=31;COUNT=3"
            }
    finally:
        app.dependency_overrides.clear()
def test_unsupported_recurrence_returns_clarification_instead_of_500(
    db, integration_engine, fixed_clock,
):
    text = "напомни завтра в 9 утра пить воду через день"
    provider = FakeLLMProvider({
        text: {
            "intent": "CREATE_EVENT",
            "event_type": "REMINDER",
            "title": "Пить воду",
            "date_expression": "завтра",
            "time_expression": "в 9 утра",
            "recurrence_expression": "через день",
        },
    })

    with client_for(integration_engine, provider) as client:
        response = client.post(
            "/api/v1/assistant/messages",
            json={"message": text},
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["requires_input"] is True
        assert payload["missing_fields"] == ["recurrence"]
        assert "правило повторения" in payload["message"].casefold()

    with Session(integration_engine) as check:
        assert check.scalars(select(Event)).all() == []