"""Soft deletion without losing event/conversation references."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE event_status ADD VALUE IF NOT EXISTS 'deleted'")


def downgrade():
    # Preserve rows when restoring the previous enum; cancelled remains inactive.
    op.execute("UPDATE events SET status = 'cancelled' WHERE status = 'deleted'")
    op.execute("ALTER TABLE events ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE event_status RENAME TO event_status_with_deleted")
    op.execute("CREATE TYPE event_status AS ENUM ('pending', 'completed', 'cancelled')")
    op.execute("ALTER TABLE events ALTER COLUMN status TYPE event_status "
               "USING status::text::event_status")
    op.execute("ALTER TABLE events ALTER COLUMN status SET DEFAULT 'pending'")
    op.execute("DROP TYPE event_status_with_deleted")
