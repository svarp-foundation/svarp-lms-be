"""Add paid certificate settings to courses and payment_type to course_payments

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-09-30 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('courses', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_certificate_paid', sa.Boolean(), nullable=True, server_default=sa.text('false')))
        batch_op.add_column(sa.Column('certificate_price', sa.Float(), nullable=True, server_default=sa.text('0.0')))

    with op.batch_alter_table('course_payments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('payment_type', sa.String(), nullable=True, server_default=sa.text("'course_enrollment'")))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('course_payments', schema=None) as batch_op:
        batch_op.drop_column('payment_type')

    with op.batch_alter_table('courses', schema=None) as batch_op:
        batch_op.drop_column('certificate_price')
        batch_op.drop_column('is_certificate_paid')
