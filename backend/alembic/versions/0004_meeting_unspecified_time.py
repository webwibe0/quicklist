"""Allow dated meetings without a chosen time; reminders still require a moment."""
import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_events_required_schedule", "events", type_="check")
    op.create_check_constraint(
        "ck_events_required_schedule", "events",
        "type = 'task' OR (type = 'meeting' AND event_date IS NOT NULL) OR "
        "(type = 'reminder' AND event_date IS NOT NULL AND event_time IS NOT NULL)",
    )


def downgrade():
    # Never invent times or drop data to satisfy the old invariant.
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM events WHERE type = 'meeting' AND event_time IS NULL)"
    )):
        raise RuntimeError("Schedule meetings without times before downgrading to 0003.")
    op.drop_constraint("ck_events_required_schedule", "events", type_="check")
    op.create_check_constraint(
        "ck_events_required_schedule", "events",
        "type = 'task' OR (event_date IS NOT NULL AND event_time IS NOT NULL)",
    )
