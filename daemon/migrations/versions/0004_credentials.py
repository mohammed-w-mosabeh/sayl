"""site credentials

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credential",
        sa.Column("host", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("credential")
