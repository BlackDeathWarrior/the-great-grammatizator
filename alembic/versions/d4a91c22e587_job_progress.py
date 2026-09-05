"""live job progress, written as the run happens

Revision ID: d4a91c22e587
Revises: c1f7a2b09d34
Create Date: 2026-09-05 15:20:00.000000
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision = 'd4a91c22e587'
down_revision = 'c1f7a2b09d34'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deliberately its own table rather than columns on `jobs`. Artefacts and
    # their QA rows are written in ONE transaction at the end of a run, and
    # that atomicity is worth keeping - a half-written artefact is worse than a
    # late one. Progress has the opposite requirement: it is only useful while
    # the run is still going, and is disposable afterwards.
    op.create_table(
        'job_progress',
        sa.Column('job_id', sa.String(length=32), nullable=False),
        sa.Column('stage', sa.String(length=32), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False, server_default=''),
        sa.Column('data', JSONB(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('job_id', 'stage', 'key'),
    )
    op.create_index('ix_job_progress_job_id', 'job_progress', ['job_id'])


def downgrade() -> None:
    op.drop_index('ix_job_progress_job_id', table_name='job_progress')
    op.drop_table('job_progress')
