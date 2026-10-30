"""Owner-bound Google OAuth and event sync state. No backfill/export of local events."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "google_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("generation", sa.Uuid(), nullable=False),
        sa.Column("google_subject", sa.Text()),
        sa.Column("account_email", sa.Text()),
        sa.Column("credentials_cipher", sa.Text()),
        sa.Column("sync_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error_code", sa.Text()),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("owner_id", "id", name="uq_google_connection_owner"),
    )
    op.create_table(
        "google_oauth_states",
        sa.Column("state_hash", sa.Text(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Uuid(), nullable=False),
        sa.Column(
            "session_hash",
            sa.Text(),
            sa.ForeignKey("auth_sessions.token_hash", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("verifier_cipher", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id", "connection_id"],
            ["google_connections.owner_id", "google_connections.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "google_event_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Uuid(), nullable=False),
        sa.Column("google_subject", sa.Text(), nullable=False),
        sa.Column("calendar_id", sa.Text(), nullable=False),
        sa.Column("remote_event_id", sa.Text(), nullable=False),
        sa.Column("desired", JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("synced_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "event_id"], ["events.owner_id", "events.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "connection_id"], ["google_connections.owner_id", "google_connections.id"]
        ),
        sa.UniqueConstraint(
            "owner_id", "event_id", "google_subject", name="uq_google_event_subject"
        ),
    )
    op.create_index("ix_google_event_links_owner_id", "google_event_links", ["owner_id"])
    op.create_index("ix_google_event_links_event_id", "google_event_links", ["event_id"])


def downgrade():
    raise RuntimeError(
        "Credential/link deletion downgrade blocked; use the approved backup recovery procedure"
    )
