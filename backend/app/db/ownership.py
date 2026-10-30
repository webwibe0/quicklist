"""Fail-closed ownership for all ORM data paths, including direct get and aliases."""

from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import TextClause


class OwnershipError(RuntimeError):
    pass


def bind_owner(session, owner_id):
    owner_id = UUID(str(owner_id))
    if session.info.get("system_access") or session.info.get("owner_id", owner_id) != owner_id:
        raise OwnershipError("Cannot change the authenticated owner of a session")
    session.info["owner_id"] = owner_id
    return session


def owned_models():
    from app.models.entities import Conversation, Event
    from app.models.google_calendar import GoogleConnection, GoogleEventLink, GoogleOAuthState
    from app.models.search import EventSearchDocument

    return Event, Conversation, EventSearchDocument, GoogleConnection, GoogleOAuthState, GoogleEventLink


@event.listens_for(Session, "do_orm_execute")
def scope_queries(state):
    if state.session.info.get("system_access"):
        return
    owner = state.session.info.get("owner_id")
    names = {"events", "conversations", "event_search_documents", 'google_connections',
             'google_oauth_states', 'google_event_links'}
    referenced = any(
        getattr(item, "name", None) in names for item in visitors.iterate(state.statement)
    )
    if referenced and (owner is None or not state.is_orm_statement):
        raise OwnershipError("An authenticated ORM owner is required")
    if owner is not None:
        if isinstance(state.statement, TextClause):
            raise OwnershipError("Raw SQL is not allowed in an owner session")
        for model in owned_models():
            state.statement = state.statement.options(
                with_loader_criteria(model, model.owner_id == owner, include_aliases=True)
            )


@event.listens_for(Session, "before_flush")
def scope_writes(session, flush_context, instances):
    if session.info.get("system_access"):
        return
    owner = session.info.get("owner_id")
    for obj in session.new | session.dirty | session.deleted:
        if isinstance(obj, owned_models()):
            if owner is None:
                raise OwnershipError("An authenticated write owner is required")
            if obj.owner_id is None and obj in session.new:
                obj.owner_id = owner
            if obj.owner_id != owner:
                raise OwnershipError("Cross-owner write rejected")
