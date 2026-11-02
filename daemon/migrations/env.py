"""Alembic environment. The database URL always comes from daemon.db.alembic_config."""

from alembic import context
from sqlalchemy import create_engine
from sqlmodel import SQLModel

import daemon.models  # noqa: F401 - registers the tables on SQLModel.metadata

with create_engine(context.config.get_main_option("sqlalchemy.url") or "").connect() as connection:
    context.configure(
        connection=connection, target_metadata=SQLModel.metadata, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()
