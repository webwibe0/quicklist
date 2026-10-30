"""Add optional vector foundation. Apply only after infrastructure approval.

Downgrade removes derived search data, never events/conversations or shared vector extension.
"""
import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "event_search_documents",
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_title", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("text_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source_title ~ '[^[:space:]]'", name="ck_search_document_title"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_search_document_hash"),
        sa.CheckConstraint("text_version > 0", name="ck_search_document_version"),
    )
    op.create_index("ix_search_document_content", "event_search_documents", ["content_hash", "text_version"])
    op.create_table(
        "embedding_cache",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("canonical_text", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False, server_default="1536"),
        sa.Column("text_version", sa.Integer(), nullable=False),
        sa.Column("embedding", VECTOR(1536)),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("content_hash", "model", "dimensions", "text_version",
                            name="uq_embedding_cache_profile_content"),
        sa.CheckConstraint("canonical_text ~ '[^[:space:]]'", name="ck_embedding_cache_text"),
        sa.CheckConstraint("content_hash = encode(sha256(convert_to(canonical_text, 'UTF8')), 'hex')",
                           name="ck_embedding_cache_hash"),
        sa.CheckConstraint("length(trim(model)) > 0", name="ck_embedding_cache_model"),
        sa.CheckConstraint("dimensions = 1536", name="ck_embedding_cache_dimensions"),
        sa.CheckConstraint("text_version > 0", name="ck_embedding_cache_version"),
        sa.CheckConstraint("status IN ('pending','processing','ready','error')", name="ck_embedding_cache_status"),
        sa.CheckConstraint("(status = 'ready') = (embedding IS NOT NULL)", name="ck_embedding_cache_ready"),
        sa.CheckConstraint("embedding IS NULL OR vector_norm(embedding) > 0", name="ck_embedding_cache_nonzero"),
        sa.CheckConstraint("attempts >= 0", name="ck_embedding_cache_attempts"),
        sa.CheckConstraint("(lease_until IS NULL) = (lease_token IS NULL) AND "
                           "(status = 'processing') = (lease_until IS NOT NULL)", name="ck_embedding_cache_lease"),
        sa.CheckConstraint("last_error_code IS NULL OR last_error_code ~ '^[A-Z0-9_]{1,64}$'",
                           name="ck_embedding_cache_error_code"),
    )
    op.create_index("ix_embedding_cache_retry", "embedding_cache", ["status", "next_retry_at"])
    # Serialize publication with concurrent rename/delete. No trigger on events itself.
    op.execute("""
        CREATE FUNCTION stage8_check_document_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM 1 FROM events WHERE id = NEW.event_id AND title = NEW.source_title
            AND status <> 'deleted' FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Search document requires a current event snapshot' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER check_search_document_snapshot BEFORE INSERT OR UPDATE
          ON event_search_documents FOR EACH ROW EXECUTE FUNCTION stage8_check_document_snapshot();
    """)


def downgrade():
    op.drop_table("event_search_documents")
    op.execute("DROP FUNCTION stage8_check_document_snapshot()")
    op.drop_table("embedding_cache")
    # Extensions can be shared. Do not DROP EXTENSION vector.
