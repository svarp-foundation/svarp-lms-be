"""change_user_id_to_string

Revision ID: 2cd8c371fcf0
Revises: f327b2d0161e
Create Date: 2026-07-22 16:17:32.136928

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2cd8c371fcf0'
down_revision: Union[str, Sequence[str], None] = 'f327b2d0161e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'audit_logs' in tables:
        with op.batch_alter_table('audit_logs', schema=None) as batch_op:
            batch_op.alter_column('admin_id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=True)

    if 'certificates' in tables:
        with op.batch_alter_table('certificates', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=True)
            batch_op.alter_column('issued_by',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=True)

    if 'course_payments' in tables:
        with op.batch_alter_table('course_payments', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=True)

    if 'enrollments' in tables:
        with op.batch_alter_table('enrollments', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=True)

    if 'lesson_comments' in tables:
        with op.batch_alter_table('lesson_comments', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=False)

    if 'lesson_completions' in tables:
        with op.batch_alter_table('lesson_completions', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=True)

    if 'submissions' in tables:
        with op.batch_alter_table('submissions', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=True)

    if 'users' in tables:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.alter_column('id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=False)

    if 'wishlists' in tables:
        with op.batch_alter_table('wishlists', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.INTEGER(),
                   type_=sa.String(),
                   existing_nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'wishlists' in tables:
        with op.batch_alter_table('wishlists', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=True)

    if 'users' in tables:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.alter_column('id',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=False)

    if 'submissions' in tables:
        with op.batch_alter_table('submissions', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=True)

    if 'lesson_completions' in tables:
        with op.batch_alter_table('lesson_completions', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=True)

    if 'lesson_comments' in tables:
        with op.batch_alter_table('lesson_comments', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=False)

    if 'enrollments' in tables:
        with op.batch_alter_table('enrollments', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=True)

    if 'course_payments' in tables:
        with op.batch_alter_table('course_payments', schema=None) as batch_op:
            batch_op.alter_column('user_id',
                   existing_type=sa.String(),
                   type_=sa.String(),
                   existing_nullable=True)

    if 'certificates' in tables:
        with op.batch_alter_table('certificates', schema=None) as batch_op:
            batch_op.alter_column('issued_by',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=True)
            batch_op.alter_column('user_id',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=True)

    if 'audit_logs' in tables:
        with op.batch_alter_table('audit_logs', schema=None) as batch_op:
            batch_op.alter_column('admin_id',
                   existing_type=sa.String(),
                   type_=sa.INTEGER(),
                   existing_nullable=True)

    # ### end Alembic commands ###
