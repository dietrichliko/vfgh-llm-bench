"""Database engine, sessions and migrations (SQLModel on SQLAlchemy, SQLite by default)."""

from collections.abc import Iterator
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


def migrate(engine: Engine) -> None:
    command.upgrade(alembic_config(engine), "head")


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session
