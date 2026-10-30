"""Email verification and one-use account recovery; preserves all existing data."""
import sqlalchemy as sa

from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True))
    # No address is silently marked verified. The existing legacy owner retains access
    # through the existing is_legacy_owner policy while being able to verify normally.
    op.create_table('account_tokens',
        sa.Column('token_hash', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('purpose', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("purpose IN ('verify','reset')", name='ck_account_token_purpose'))
    op.create_index('ix_account_tokens_user_id', 'account_tokens', ['user_id'])
    op.create_index('ix_account_tokens_expires_at', 'account_tokens', ['expires_at'])


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT EXISTS(SELECT 1 FROM account_tokens) OR EXISTS(SELECT 1 FROM users WHERE email_verified_at IS NOT NULL)')):
        raise RuntimeError('Account security state exists; reviewed restore required before downgrade')
    op.drop_table('account_tokens')
    op.drop_column('users', 'email_verified_at')
