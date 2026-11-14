"""schedule enabled

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedule") as batch_op:
        batch_op.add_column(sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"))


def downgrade() -> None:
    with op.batch_alter_table("schedule") as batch_op:
        batch_op.drop_column("enabled")
