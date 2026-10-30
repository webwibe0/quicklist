"""Reported target-date and pending-state failures; no paid providers or primary DB."""

import json
from datetime import date, time
from uuid import uuid4

import pytest

from app.models.entities import Event
from tests import test_user_regressions
from tests.test_user_regressions import NOW

harness = test_user_regressions.harness


def occurrences(session):
    series = uuid4()
    rows = [
        Event(
            type="task",
            title="постригать ногти",
            event_date=date(2026, 11, day),
            event_time=time(10),
            recurrence_series_id=series,
            recurrence_rule="FREQ=WEEKLY;BYDAY=FR",
            recurrence_until=date(2026, 11, 27),
        )
        for day in (6, 13, 20, 27)
    ]
    session.add_all(rows)
    session.flush()
    return rows


@pytest.mark.parametrize("field", ["period_expression", "date_expression"])
def test_exact_rename_uses_old_date_and_changes_only_one_occurrence(harness, field):
    service, conversation, session, provider = harness
    rows = occurrences(session)
    text = "замени постригать ногти 20 ноября на стричь волосы"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "title": "постригать ногти",
        "new_title": "стричь волосы",
        field: "20 ноября",
    }
    reply = service.handle(conversation, text, NOW)
    assert not reply.requires_input
    assert [e.id for e in reply.events] == [rows[2].id]
    session.expire_all()
    assert [(r.title, r.event_date) for r in rows] == [
        ("стричь волосы" if i == 2 else "постригать ногти", date(2026, 11, d))
        for i, d in enumerate((6, 13, 20, 27))
    ]
    assert conversation.pending_action is None


@pytest.mark.parametrize("answer", ["20.11.2026", "постригать ногти, 20.11.2026"])
def test_date_clarification_preserves_rename_after_serialization(harness, answer):
    service, conversation, session, provider = harness
    rows = occurrences(session)
    text = "замени постригать ногти на стричь волосы"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "title": "постригать ногти",
        "new_title": "стричь волосы",
    }
    first = service.handle(conversation, text, NOW)
    assert first.requires_input and len(first.events) == 4
    # Same title is insufficient: do not post the same four cards a second time.
    provider.commands["постригать ногти"] = {"intent": "UPDATE_EVENT", "title": "постригать ногти"}
    again = service.handle(conversation, "постригать ногти", NOW)
    assert again.requires_input and again.events == []
    conversation.pending_action = json.loads(json.dumps(conversation.pending_action))
    conversation.context = json.loads(json.dumps(conversation.context))
    count = len(provider.contexts)
    reply = service.handle(conversation, answer, NOW)
    assert not reply.requires_input and len(provider.contexts) == count
    assert [e.id for e in reply.events] == [rows[2].id]
    assert rows[2].title == "стричь волосы" and rows[2].event_date == date(2026, 11, 20)
    assert conversation.pending_action is None


def test_destination_date_never_filters_existing_update_target(harness):
    service, conversation, session, provider = harness
    row = Event(type="task", title="обед", event_date=date(2026, 10, 16), event_time=time(13))
    session.add(row)
    session.flush()
    text = "перенеси обед на воскресенье"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "title": "обед",
        "date_expression": "воскресенье",
    }
    reply = service.handle(conversation, text, NOW)
    assert not reply.requires_input and row.event_date == date(2026, 10, 18)


def test_existing_guarded_retrieval_offers_semantic_candidate_then_numeric_choice(harness):
    service, conversation, session, provider = harness
    row = Event(
        type="task", title="счёт за хостинг", event_date=date(2026, 10, 16), event_time=time(13)
    )
    session.add(row)
    session.flush()

    class VerifiedSearch:
        def __init__(self):
            self.commands = []

        def execute(self, command):
            self.commands.append(command)
            return [row]

    search = VerifiedSearch()
    service.targets.search = search
    text = "перенеси оплату сервера на воскресенье"
    provider.commands[text] = {
        "intent": "UPDATE_EVENT",
        "title": "оплату сервера",
        "date_expression": "воскресенье",
    }
    first = service.handle(conversation, text, NOW)
    assert first.requires_input and [e.id for e in first.events] == [row.id]
    assert row.event_date == date(2026, 10, 16)  # Semantic proposal is never a mutation.
    assert search.commands[0].query == "оплату сервера"  # No invented retrieval facts.
    conversation.pending_action = json.loads(json.dumps(conversation.pending_action))
    count = len(provider.contexts)
    final = service.handle(conversation, "1", NOW)
    assert not final.requires_input and len(provider.contexts) == count
    assert row.event_date == date(2026, 10, 18)


def test_ambiguous_clock_is_not_silently_absolutized_by_nlu(harness):
    service, conversation, session, provider = harness
    text = "пообедать завтра в час"
    provider.commands[text] = {
        "intent": "CREATE_EVENT",
        "event_type": "TASK",
        "title": "пообедать",
        "date_expression": "завтра",
        "time_expression": "01:00",
    }
    reply = service.handle(conversation, text, NOW)
    assert reply.requires_input and reply.missing_fields == ["time"]
    assert "01:00" in reply.message and "13:00" in reply.message
    assert list(session.query(Event)) == []


@pytest.mark.parametrize(
    "query,title",
    [
        ("обед", "пообедать"),
        ("созвон", "позвонить"),
        ("встреча с Иваном", "Встреча с Иваном"),
        ("оплата сервера", "счёт за хостинг"),
    ],
)
def test_existing_hybrid_guard_keeps_verified_paraphrases(query, title):
    # Scripted retrieval tests the existing guard/wiring, not embedding model quality.
    from tests.test_hybrid_search import event, search

    row = event(title)
    result, service, _ = search([], [row], query)
    assert result == [row] and service.result.states[str(row.id)] == "VERIFIED"


def test_semantic_proposals_never_widen_explicit_context_or_cross_owner(harness):
    from sqlalchemy.orm import Session

    from app.schemas.commands import ParsedCommand
    from app.schemas.dialogue import DialogueContext
    from app.services.targets import EventTargetResolver
    from app.temporal.resolver import TemporalResult

    service, conversation, session, provider = harness
    owned = Event(type="task", title="счёт за хостинг", event_date=date(2026, 10, 16))
    session.add(owned)
    session.flush()
    other_owner = uuid4()
    with Session(session.get_bind(), info={"system_access": True}, expire_on_commit=False) as seed:
        other = Event(
            owner_id=other_owner, type="task", title="оплата сервера", event_date=date(2026, 10, 16)
        )
        seed.add(other)
        seed.commit()

    class PoisonedRetrieval:
        def execute(self, command):
            return [other]

    resolver = EventTargetResolver(service.router.repository, PoisonedRetrieval())
    command = ParsedCommand(intent="UPDATE_EVENT", title="оплата сервера")
    assert resolver.resolve(command, TemporalResult(), DialogueContext(), "оплата сервера") == []
    assert (
        resolver.resolve(
            command, TemporalResult(), DialogueContext(), "оплата сервера", candidate_ids=[]
        )
        == []
    )


def test_explicit_am_pm_and_unrelated_invented_time_still_use_strict_guard(harness):
    from app.ai.provider import ProviderError

    service, conversation, session, provider = harness
    for suffix, hour in [("дня", 13), ("ночи", 1)]:
        text = "пообедать завтра в час " + suffix
        provider.commands[text] = {
            "intent": "CREATE_EVENT",
            "event_type": "TASK",
            "title": "пообедать",
            "date_expression": "завтра",
            "time_expression": "в час " + suffix,
        }
        reply = service.handle(conversation, text, NOW)
        assert not reply.requires_input and reply.events[0].event_time == time(hour)
    provider.commands["создай задачу без времени"] = {
        "intent": "CREATE_EVENT",
        "event_type": "TASK",
        "title": "без времени",
        "time_expression": "01:00",
    }
    with pytest.raises(ProviderError, match="неуказанную"):
        service.handle(conversation, "создай задачу без времени", NOW)
