"""Alembic environment: the engine comes from bench.db.migrate (no alembic.ini)."""

from alembic import context
from sqlmodel import SQLModel

import bench.models  # noqa: F401  (registers the tables)

engine = context.config.attributes["engine"]

with engine.begin() as connection:
    context.configure(
        connection=connection,
        target_metadata=SQLModel.metadata,
        render_as_batch=True,  # SQLite cannot ALTER most things
    )
    context.run_migrations()
