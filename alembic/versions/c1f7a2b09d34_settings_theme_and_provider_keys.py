"""operator theme preference and runtime provider keys

Revision ID: c1f7a2b09d34
Revises: 2ec3399d0a5b
Create Date: 2026-09-05 17:10:00.000000
"""
import sqlalchemy as sa

from alembic import op


revision = 'c1f7a2b09d34'
down_revision = '2ec3399d0a5b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Appearance is a per-operator preference, not learned data, so it sits on
    # the profile rather than in style_notes.
    op.add_column(
        'operator_profiles',
        sa.Column('theme', sa.String(length=16), nullable=False, server_default='system'),
    )

    # Runtime provider keys. Ciphertext only - the plaintext never leaves the
    # gateway, and the browser is only ever shown `hint`.
    op.create_table(
        'provider_keys',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('hint', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', name='uq_provider_keys_provider'),
    )

    # A single row whose version the worker watches: bumping it is how a key
    # saved in the browser reaches a process in another container without a
    # restart.
    op.create_table(
        'provider_key_version',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.execute('INSERT INTO provider_key_version (id, version) VALUES (1, 0)')


def downgrade() -> None:
    op.drop_table('provider_key_version')
    op.drop_table('provider_keys')
    op.drop_column('operator_profiles', 'theme')
