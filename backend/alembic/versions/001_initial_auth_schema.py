
"""Initial auth schema

Revision ID: 001_initial_auth_schema
Revises: 
Create Date: 2024-01-01 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '001_initial_auth_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create users table
    op.create_table('users',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('primary_email_id', sa.String(length=36), nullable=True),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('avatar_url', sa.String(length=500), nullable=True),
    sa.Column('locale', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )

    # Create emails table
    op.create_table('emails',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('is_verified', sa.Boolean(), nullable=False),
    sa.Column('is_primary', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_emails_email'), 'emails', ['email'], unique=True)
    op.create_index(op.f('ix_emails_user_id'), 'emails', ['user_id'], unique=False)

    # Add foreign key to users.primary_email_id
    op.create_foreign_key(None, 'users', 'emails', ['primary_email_id'], ['id'])

    # Create password_credentials table
    op.create_table('password_credentials',
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('password_version', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )

    # Create organisations table
    op.create_table('organisations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('owner_user_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_organisations_slug'), 'organisations', ['slug'], unique=True)

    # Create roles table
    op.create_table('roles',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('org_id', sa.String(length=36), nullable=True),
    sa.Column('key', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('is_system', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organisations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )

    # Create permissions table
    op.create_table('permissions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('key', sa.String(length=50), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_permissions_key'), 'permissions', ['key'], unique=True)

    # Create sessions table
    op.create_table('sessions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('device_id', sa.String(length=32), nullable=False),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('ip_first', sa.String(length=45), nullable=False),
    sa.Column('ip_last', sa.String(length=45), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sessions_device_id'), 'sessions', ['device_id'], unique=False)
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_index('ix_sessions_user_device', 'sessions', ['user_id', 'device_id'], unique=False)

    # Create role_permissions table
    op.create_table('role_permissions',
    sa.Column('role_id', sa.String(length=36), nullable=False),
    sa.Column('permission_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['permission_id'], ['permissions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('role_id', 'permission_id')
    )

    # Create org_members table
    op.create_table('org_members',
    sa.Column('org_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('role_id', sa.String(length=36), nullable=False),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('invited_by', sa.String(length=36), nullable=True),
    sa.ForeignKeyConstraint(['invited_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['org_id'], ['organisations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('org_id', 'user_id')
    )

    # Create refresh_tokens table
    op.create_table('refresh_tokens',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reuse_flag', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_refresh_tokens_expires_at'), 'refresh_tokens', ['expires_at'], unique=False)
    op.create_index(op.f('ix_refresh_tokens_session_id'), 'refresh_tokens', ['session_id'], unique=False)
    op.create_index(op.f('ix_refresh_tokens_token_hash'), 'refresh_tokens', ['token_hash'], unique=True)
    op.create_index('ix_refresh_tokens_expires', 'refresh_tokens', ['expires_at'], unique=False)

    # Create audit_logs table
    op.create_table('audit_logs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=True),
    sa.Column('actor_user_id', sa.String(length=36), nullable=True),
    sa.Column('event', sa.String(length=50), nullable=False),
    sa.Column('ip', sa.String(length=45), nullable=False),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('payload_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_logs_created_at'), 'audit_logs', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_logs_event'), 'audit_logs', ['event'], unique=False)
    op.create_index(op.f('ix_audit_logs_user_id'), 'audit_logs', ['user_id'], unique=False)
    op.create_index('ix_audit_logs_user_event', 'audit_logs', ['user_id', 'event'], unique=False)

    # Create password_resets table
    op.create_table('password_resets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_password_resets_expires_at'), 'password_resets', ['expires_at'], unique=False)
    op.create_index(op.f('ix_password_resets_token_hash'), 'password_resets', ['token_hash'], unique=True)
    op.create_index(op.f('ix_password_resets_user_id'), 'password_resets', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_table('password_resets')
    op.drop_table('audit_logs')
    op.drop_table('refresh_tokens')
    op.drop_table('org_members')
    op.drop_table('role_permissions')
    op.drop_table('sessions')
    op.drop_table('permissions')
    op.drop_table('roles')
    op.drop_table('organisations')
    op.drop_table('password_credentials')
    op.drop_table('emails')
    op.drop_table('users')
