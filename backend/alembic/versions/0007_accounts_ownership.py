"""Explicit original-owner binding, accounts and tenant foreign keys; no vector rebuild."""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import context, op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    args = context.get_x_argument(as_dictionary=True)
    args.update(context.config.attributes.get("legacy_owner", {}))
    if args.get("legacy_owner_confirmed") != "yes":
        raise RuntimeError(
            "Explicit legacy_owner_confirmed=yes is required before any migration write"
        )
    owner_id = UUID(args["legacy_owner_id"])
    email = args["legacy_owner_email"].strip().casefold()
    if "@" not in email or len(email) > 254:
        raise RuntimeError("An explicit original owner email is required")
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("is_legacy_owner", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("email = lower(trim(email))", name="ck_users_email"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("csrf_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table(
        "auth_throttles",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Reserved account cannot be claimed by registration; password is set locally later.
    op.execute(
        sa.text(
            "INSERT INTO users (id,email,is_legacy_owner) VALUES (:owner,:email,true)"
        ).bindparams(owner=owner_id, email=email)
    )
    for table in ("events", "conversations", "event_search_documents"):
        op.add_column(table, sa.Column("owner_id", sa.Uuid(), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET owner_id=:owner").bindparams(owner=owner_id))
        op.alter_column(table, "owner_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_owner", table, "users", ["owner_id"], ["id"], ondelete="RESTRICT"
        )
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])
    op.add_column("conversations", sa.Column("messages", JSONB(none_as_null=True), nullable=True))
    op.create_unique_constraint("uq_events_owner_id", "events", ["owner_id", "id"])
    op.create_unique_constraint("uq_conversations_owner_id", "conversations", ["owner_id", "id"])
    op.drop_constraint("uq_events_source_id", "events", type_="unique")
    op.create_unique_constraint(
        "uq_events_source_id", "events", ["owner_id", "source", "source_id"]
    )
    inspector = sa.inspect(op.get_bind())
    for table, columns in [
        ("conversations", ["last_event_id"]),
        ("event_search_documents", ["event_id"]),
    ]:
        for foreign in inspector.get_foreign_keys(table):
            if foreign["constrained_columns"] == columns:
                op.drop_constraint(foreign["name"], table, type_="foreignkey")
    op.create_foreign_key(
        "fk_conversation_owned_last_event",
        "conversations",
        "events",
        ["owner_id", "last_event_id"],
        ["owner_id", "id"],
    )
    op.create_foreign_key(
        "fk_document_owned_event",
        "event_search_documents",
        "events",
        ["owner_id", "event_id"],
        ["owner_id", "id"],
        ondelete="CASCADE",
    )


def downgrade():
    raise RuntimeError(
        "Data-destructive downgrade blocked. Use the verified backup recovery runbook before reopening writes."
    )
