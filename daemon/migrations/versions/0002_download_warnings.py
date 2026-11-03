"""download warnings

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("download") as batch_op:
        batch_op.add_column(
            sa.Column("warnings_json", sa.String(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("download") as batch_op:
        batch_op.drop_column("warnings_json")
