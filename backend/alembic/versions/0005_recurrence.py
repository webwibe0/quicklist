"""Add recurrence metadata to events."""
import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("recurrence_rule", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("recurrence_series_id", sa.Uuid(), nullable=True))
    op.add_column("events", sa.Column("recurrence_until", sa.Date(), nullable=True))
    op.create_index(
        "ix_events_recurrence_series_id",
        "events",
        ["recurrence_series_id"],
    )


def downgrade():
    op.drop_index("ix_events_recurrence_series_id", table_name="events")
    op.drop_column("events", "recurrence_until")
    op.drop_column("events", "recurrence_series_id")
    op.drop_column("events", "recurrence_rule")