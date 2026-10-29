import gzip
import sqlite3
from pathlib import Path

from bench.backup import backup_sqlite
from bench.db import make_engine, migrate


def test_backup_is_a_complete_database(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'bench.sqlite'}"
    migrate(make_engine(url))
    packed = backup_sqlite(url, tmp_path / "backups", "test")
    assert packed.name.endswith("-test.sqlite.gz")
    restored = tmp_path / "restored.sqlite"
    restored.write_bytes(gzip.decompress(packed.read_bytes()))
    with sqlite3.connect(restored) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "audit_log", "provider_checks", "alembic_version"} <= tables
