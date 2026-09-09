"""Add performance indexes for common query patterns

Revision ID: a1b2c3d4e5f6
Revises: 278c9465704f
Create Date: 2026-09-09 15:18:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '5e91b2c47a0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Add indexes to critical filter/join columns that are currently unindexed.
    These directly impact the slowest API endpoints:
    - /admin/analytics (enrollments, certificates, payments aggregations)
    - /admin/submissions (user + assignment joins)
    - /learner/courses/content (lesson completions)
    - /instructor/courses (enrollment counts per course)
    """

    # Enrollment lookups — the most common join pattern across admin, instructor, learner
    op.create_index(
        'ix_enrollments_user_course',
        'enrollments',
        ['user_id', 'course_id'],
        unique=True,
        if_not_exists=True
    )

    # Submission filtering by status, user, and assignment
    op.create_index(
        'ix_submissions_user_id',
        'submissions',
        ['user_id'],
        if_not_exists=True
    )
    op.create_index(
        'ix_submissions_status',
        'submissions',
        ['status'],
        if_not_exists=True
    )
    op.create_index(
        'ix_submissions_assignment_id',
        'submissions',
        ['assignment_id'],
        if_not_exists=True
    )

    # Lesson completion lookups (used by progress calculation)
    op.create_index(
        'ix_lesson_completions_user_lesson',
        'lesson_completions',
        ['user_id', 'lesson_id'],
        if_not_exists=True
    )

    # Course payment filtering (analytics revenue calculations)
    op.create_index(
        'ix_course_payments_status',
        'course_payments',
        ['status'],
        if_not_exists=True
    )
    op.create_index(
        'ix_course_payments_user_course',
        'course_payments',
        ['user_id', 'course_id'],
        if_not_exists=True
    )

    # Certificate lookups (completion checks, analytics)
    op.create_index(
        'ix_certificates_user_course',
        'certificates',
        ['user_id', 'course_id'],
        if_not_exists=True
    )

    # Module → course lookups (joins in lesson/progress queries)
    op.create_index(
        'ix_modules_course_id',
        'modules',
        ['course_id'],
        if_not_exists=True
    )

    # Lesson → module lookups (joins in content/progress queries)
    op.create_index(
        'ix_lessons_module_id',
        'lessons',
        ['module_id'],
        if_not_exists=True
    )

    # Assignment lookups (submission joins, final assignment checks)
    op.create_index(
        'ix_assignments_course_id',
        'assignments',
        ['course_id'],
        if_not_exists=True
    )
    op.create_index(
        'ix_assignments_lesson_id',
        'assignments',
        ['lesson_id'],
        if_not_exists=True
    )

    # Course filtering (is_deleted + status — used in nearly every course query)
    op.create_index(
        'ix_courses_status_deleted',
        'courses',
        ['status', 'is_deleted'],
        if_not_exists=True
    )


def downgrade() -> None:
    """Remove all performance indexes."""
    op.drop_index('ix_courses_status_deleted', table_name='courses', if_exists=True)
    op.drop_index('ix_assignments_lesson_id', table_name='assignments', if_exists=True)
    op.drop_index('ix_assignments_course_id', table_name='assignments', if_exists=True)
    op.drop_index('ix_lessons_module_id', table_name='lessons', if_exists=True)
    op.drop_index('ix_modules_course_id', table_name='modules', if_exists=True)
    op.drop_index('ix_certificates_user_course', table_name='certificates', if_exists=True)
    op.drop_index('ix_course_payments_user_course', table_name='course_payments', if_exists=True)
    op.drop_index('ix_course_payments_status', table_name='course_payments', if_exists=True)
    op.drop_index('ix_lesson_completions_user_lesson', table_name='lesson_completions', if_exists=True)
    op.drop_index('ix_submissions_assignment_id', table_name='submissions', if_exists=True)
    op.drop_index('ix_submissions_status', table_name='submissions', if_exists=True)
    op.drop_index('ix_submissions_user_id', table_name='submissions', if_exists=True)
    op.drop_index('ix_enrollments_user_course', table_name='enrollments', if_exists=True)
