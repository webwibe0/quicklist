"""Persist completed-event dialogue context separately from pending actions."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("context", postgresql.JSONB(none_as_null=True)))


def downgrade():
    op.drop_column("conversations", "context")
