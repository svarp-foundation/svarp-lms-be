"""Add instructor panel and applications

Revision ID: 5e91b2c47a0d
Revises: 278c9465704f
Create Date: 2026-09-09 11:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e91b2c47a0d'
down_revision: Union[str, Sequence[str], None] = '278c9465704f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Create instructor_applications table
    op.create_table(
        'instructor_applications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('specialty', sa.String(), nullable=True),
        sa.Column('bio', sa.Text(), nullable=True),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('admin_feedback', sa.Text(), nullable=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_by', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('instructor_applications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_instructor_applications_id'), ['id'], unique=False)

    # 2. Add instructor_id to courses
    with op.batch_alter_table('courses', schema=None) as batch_op:
        batch_op.add_column(sa.Column('instructor_id', sa.String(), nullable=True))
        batch_op.create_foreign_key('fk_courses_instructor_id', 'users', ['instructor_id'], ['id'])

    # 3. Add passing_score and graded_by to submissions
    with op.batch_alter_table('submissions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('passing_score', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('graded_by', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('submissions', schema=None) as batch_op:
        batch_op.drop_column('graded_by')
        batch_op.drop_column('passing_score')

    with op.batch_alter_table('courses', schema=None) as batch_op:
        batch_op.drop_constraint('fk_courses_instructor_id', type_='foreignkey')
        batch_op.drop_column('instructor_id')

    with op.batch_alter_table('instructor_applications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_instructor_applications_id'))

    op.drop_table('instructor_applications')
