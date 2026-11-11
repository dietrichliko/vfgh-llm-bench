"""Database engine, sessions and migrations (SQLModel on SQLAlchemy, SQLite by default)."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

MIGRATIONS = Path(__file__).parent / "migrations"


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url, connect_args={"check_same_thread": False} if "sqlite" in url else {}
    )
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")  # web and worker share the file
            cur.close()

    return engine


def alembic_config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["engine"] = engine
    return cfg


@contextmanager
def migration_lock(engine: Engine) -> Iterator[None]:
    """One process migrates at a time; the others wait, then find nothing left to do.

    Web and worker share the SQLite file and both migrate at start. Without the lock a
    second process ran the same migration in parallel and failed ("table … already
    exists"). The lock file sits next to the database, so it also works across containers
    that share the volume. Not needed (and skipped) for in-memory or non-SQLite databases.
    """
    path = engine.url.database if engine.dialect.name == "sqlite" else None
    if not path or path == ":memory:":
        yield
        return
    lock = Path(path).with_name(Path(path).name + ".migrate.lock")
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def migrate(engine: Engine) -> None:
    with migration_lock(engine):
        command.upgrade(alembic_config(engine), "head")


def pending_migration(engine: Engine) -> bool:
    """True if the database has a schema that a migration would change (not if empty)."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config(engine)).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    return current is not None and current != head


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session
