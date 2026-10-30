from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai.openai_provider import get_provider
from app.ai.provider import LLMProvider, ProviderError
from app.api.google_calendar import capture, configured, drain_background
from app.core.config import get_settings
from app.core.version import LOADED_CODE_VERSION, source_version
from app.db.ownership import bind_owner
from app.db.session import get_session
from app.repositories.conversations import ConversationNotFound, ConversationRepository
from app.repositories.events import EventRepository
from app.schemas.commands import EventActionRequest, Intent, MessageRequest, MessageResponse
from app.schemas.planner import TodayPlanResponse
from app.services.authentication import Identity, require_user
from app.services.dialogue import DialogueService
from app.services.hybrid_search import HybridSearch
from app.services.message_transaction import execute_message
from app.services.responses import ResponseBuilder
from app.services.today_plan import build_today_plan
from app.temporal.resolver import TemporalResolver
from app.use_cases.actions import ActionRouter

router = APIRouter()


def get_plan_now():
    return datetime.now(UTC)


@router.get('/api/v1/events/today', response_model=TodayPlanResponse)
def today_plan(session: Session = Depends(get_session), user: Identity = Depends(require_user),
               now: datetime = Depends(get_plan_now)):
    bind_owner(session, user.id)
    return build_today_plan(EventRepository(session, get_settings().result_limit),
                            get_settings().app_timezone, now)


def dialogue_service(session, provider):
    settings = get_settings()
    repository = EventRepository(session, settings.result_limit)
    search = HybridSearch(repository, settings, provider)
    return DialogueService(
        provider, TemporalResolver(settings.app_timezone),
        ActionRouter(repository, search=search), ResponseBuilder(settings.result_limit),
    )


@router.get("/health")
def health(request: Request, session: Session = Depends(get_session), provider: LLMProvider = Depends(get_provider)):
    try:
        session.connection().execute(text("SELECT 1"))
        # Ensure the schema is migrated, not merely that PostgreSQL is reachable.
        session.connection().execute(text("SELECT id FROM events LIMIT 0"))
        session.connection().execute(text("SELECT owner_id FROM conversations LIMIT 0"))
        session.connection().execute(text("SELECT id FROM users LIMIT 0"))
        session.connection().execute(text("SELECT email_verified_at FROM users LIMIT 0"))
        if get_settings().app_environment != 'local':
            session.connection().execute(text('SELECT token_hash FROM account_tokens LIMIT 0'))
            if session.connection().scalar(text('SELECT version_num FROM alembic_version')) != '0009':
                raise HTTPException(503, 'Версия схемы не соответствует приложению.')
    except SQLAlchemyError as exc:
        raise HTTPException(503, "База данных или схема недоступна.") from exc
    settings = get_settings()
    result = {"status": "ok", "database": "ok", "llm_configured": bool(
        settings.openai_api_key.get_secret_value()), "model": settings.openai_model,
        "test_mode": bool(getattr(provider, "is_test_double", False)),
        "code_version": LOADED_CODE_VERSION, "source_version": source_version(),
        "semantic_search_enabled": settings.semantic_search_enabled,
        "indexing": {"enabled": settings.embedding_worker_enabled,
            "status": getattr(getattr(request.app.state, 'indexing_worker', None), 'status', 'not_started')}}
    if not settings.embedding_worker_in_web:
        import json
        import time
        try:
            state = json.loads(settings.worker_status_path.read_text())
            status = state['status'] if time.time() - state['updated_at'] < 90 else 'unavailable'
            result['indexing']['status'] = status
        except (OSError, ValueError, KeyError, TypeError):
            result['indexing']['status'] = 'unavailable'
    if settings.app_environment != 'local':
        for key in ('code_version', 'source_version', 'model'):
            result.pop(key, None)
    return result


@router.post("/api/v1/assistant/messages", response_model=MessageResponse)
def message(request: MessageRequest, background_tasks: BackgroundTasks, session: Session = Depends(get_session),
            provider: LLMProvider = Depends(get_provider), user: Identity = Depends(require_user)):
    if not request.message.strip():
        raise HTTPException(422, "Сообщение не должно быть пустым.")
    try:
        bind_owner(session, user.id)
        capture(session, get_settings())
        result = execute_message(session, provider, dialogue_service, request)
        if configured(get_settings()):
            background_tasks.add_task(drain_background, user.id)
        return result
    except ConversationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(503, str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "База данных временно недоступна. Попробуйте ещё раз.") from exc


@router.post("/api/v1/events/{event_id}/actions", response_model=MessageResponse)
def event_action(event_id: UUID, request: EventActionRequest, background_tasks: BackgroundTasks, session: Session = Depends(get_session),
                 provider: LLMProvider = Depends(get_provider), user: Identity = Depends(require_user)):
    try:
        bind_owner(session, user.id)
        capture(session, get_settings())
        with session.begin():
            if not EventRepository(session, 1).get_action_events([event_id], Intent(request.action)):
                raise HTTPException(404, "Событие не найдено.")
            conversation = ConversationRepository(session).get_or_create(request.conversation_id)
            result = dialogue_service(session, provider).handle_event_action(
                conversation, event_id, Intent(request.action),
            )
            event = EventRepository(session, 1).get_action_events([event_id], Intent(request.action))[0]
            label = ("Удалить" if request.action == "DELETE_EVENT" else
                     "Отметить состоявшейся" if event.type == "meeting" else "Отметить выполненным")
            conversation.messages = [*(conversation.messages or []),
                {"role": "user", "text": f"{label}: {event.title}"},
                {"role": "assistant", "text": result.message, "response": result.model_dump(mode="json")}]
        if configured(get_settings()):
            background_tasks.add_task(drain_background, user.id)
        return result
    except ConversationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "База данных временно недоступна. Попробуйте ещё раз.") from exc
