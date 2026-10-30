from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def build_engine(database_url):
    return create_engine(database_url, pool_pre_ping=True, connect_args={
        "connect_timeout": make_url(database_url).query.get(
            "connect_timeout", get_settings().db_connect_timeout_seconds,
        ),
    })


engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_session():
    with SessionLocal() as session:
        # The write entrypoint owns begin/commit, before returning an HTTP response.
        # Closing still rolls back any unfinished read or failed transaction.
        yield session

# Register ownership enforcement for request and direct ORM access.
from app.db import ownership  # noqa: E402,F401
