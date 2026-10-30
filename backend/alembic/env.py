from sqlalchemy import create_engine

from alembic import context
from app.core.config import get_settings
from app.db.session import Base
from app.models import (
    entities,  # noqa: F401
    google_calendar,  # noqa: F401
    search,  # noqa: F401
)

target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
