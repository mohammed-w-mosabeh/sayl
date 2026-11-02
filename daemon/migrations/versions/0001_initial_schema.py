"""initial schema

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "category",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("extensions", sa.String(), nullable=False),
        sa.Column("mime_prefixes", sa.String(), nullable=False),
        sa.Column("dest_dir", sa.String(), nullable=False),
        sa.Column("icon", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "hostpolicy",
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("accelerate", sa.Boolean(), nullable=False),
        sa.Column(
            "reason",
            sa.Enum("AUTH", "GONE", "VALIDATOR_CHANGED", "NOT_206", name="policyreason"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("host"),
    )
    op.create_table(
        "schedule",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("cron", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("params_json", sa.String(), nullable=False),
        sa.Column("post_action", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_json", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "download",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("final_url", sa.String(), nullable=False),
        sa.Column("page_url", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("dest_dir", sa.String(), nullable=False),
        sa.Column("category_id", sa.String(), nullable=True),
        sa.Column("engine", sa.Enum("ARIA2", "YTDLP", "HYBRID", name="enginename"), nullable=False),
        sa.Column("engine_ref", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "QUEUED",
                "PROBING",
                "ACTIVE",
                "PAUSED",
                "COMPLETED",
                "FAILED",
                "CANCELED",
                "NEEDS_REFRESH",
                name="status",
            ),
            nullable=False,
        ),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("done_bytes", sa.Integer(), nullable=False),
        sa.Column("speed", sa.Integer(), nullable=False),
        sa.Column("eta", sa.Integer(), nullable=False),
        sa.Column("connections", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("speed_limit", sa.Integer(), nullable=False),
        sa.Column("format_id", sa.String(), nullable=False),
        sa.Column("media_json", sa.String(), nullable=False),
        sa.Column("etag", sa.String(), nullable=False),
        sa.Column("last_modified", sa.String(), nullable=False),
        sa.Column("resumable", sa.Boolean(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("headers_enc", sa.LargeBinary(), nullable=False),
        sa.Column("checksum_algo", sa.String(), nullable=False),
        sa.Column("checksum_expected", sa.String(), nullable=False),
        sa.Column("checksum_ok", sa.Boolean(), nullable=True),
        sa.Column("error", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_download_status"), ["status"], unique=False)

    op.create_table(
        "segment",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("download_id", sa.String(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("end", sa.Integer(), nullable=False),
        sa.Column("done_bytes", sa.Integer(), nullable=False),
        sa.Column("source", sa.Enum("STREAM", "RANGE", name="segmentsource"), nullable=False),
        sa.Column(
            "status", sa.Enum("PENDING", "ACTIVE", "DONE", name="segmentstatus"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["download_id"],
            ["download.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("segment", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_segment_download_id"), ["download_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("segment", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_segment_download_id"))

    op.drop_table("segment")
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_download_status"))

    op.drop_table("download")
    op.drop_table("setting")
    op.drop_table("schedule")
    op.drop_table("hostpolicy")
    op.drop_table("category")
