"""M1 events and conversations.

Revision ID: 0001
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("type", sa.Enum("task", "reminder", "meeting", name="event_type"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Date()),
        sa.Column("event_time", sa.Time()),
        sa.Column("end_time", sa.Time()),
        sa.Column("status", sa.Enum("pending", "completed", "cancelled", name="event_status"),
                  nullable=False, server_default="pending"),
        sa.Column("raw_user_text", sa.Text()),
        sa.Column("source", sa.Enum("user", "dataset", name="event_source"),
                  nullable=False, server_default="user"),
        sa.Column("source_id", sa.Text()),
        sa.Column("type_inferred", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("source", "source_id", name="uq_events_source_id"),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_events_title"),
        sa.CheckConstraint("type = 'task' OR (event_date IS NOT NULL AND event_time IS NOT NULL)",
                           name="ck_events_required_schedule"),
    )
    op.create_index("ix_events_date", "events", ["event_date"])
    op.execute("CREATE INDEX ix_events_title_trgm ON events USING gin (title gin_trgm_ops)")
    op.execute("CREATE INDEX ix_events_title_fts ON events USING gin "
               "(to_tsvector('russian', title))")
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("last_event_id", sa.Uuid(), sa.ForeignKey("events.id")),
        sa.Column("pending_action", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("conversations")
    op.drop_table("events")
    for name in ("event_source", "event_status", "event_type"):
        op.execute(f"DROP TYPE {name}")
    # pg_trgm can be shared by other applications; don't remove the extension.
