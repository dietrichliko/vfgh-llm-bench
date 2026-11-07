import gzip
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command

from bench.backup import (
    RemoteBackup,
    apply_pending_restore,
    backup_sqlite,
    make_backup,
    offsite_backup,
    pending_path,
    prune_plan,
    stage_restore,
    store_from_settings,
    taken_at,
)
from bench.db import alembic_config, make_engine, migrate, pending_migration
from bench.settings import Settings


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


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload(self, path: Path) -> str:
        key = "backups/" + path.name
        self.objects[key] = path.read_bytes()
        return key

    def list(self) -> list[RemoteBackup]:
        found = [RemoteBackup(k, t, len(v)) for k, v in self.objects.items() if (t := taken_at(k))]
        return sorted(found, key=lambda b: b.taken)

    def delete(self, keys: Iterable[str]) -> None:
        for k in keys:
            del self.objects[k]

    def download(self, key: str, dest: Path) -> None:
        dest.write_bytes(self.objects[key])


@pytest.fixture
def db_settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    migrate(make_engine(s.db_url))
    return s


def _write(settings: Settings, sql: str) -> None:
    with sqlite3.connect(settings.db_url.removeprefix("sqlite:///")) as db:
        db.execute(sql)


def test_an_unchanged_database_gives_the_same_hash(db_settings: Settings, tmp_path: Path) -> None:
    _, a = make_backup(db_settings.db_url, tmp_path / "b", "one")
    _, b = make_backup(db_settings.db_url, tmp_path / "b", "two")
    assert a == b
    _write(
        db_settings, "INSERT INTO audit_log (at, action, detail) VALUES ('2026-01-01', 'x', '{}')"
    )
    _, c = make_backup(db_settings.db_url, tmp_path / "b", "three")
    assert c != a


def test_offsite_backup_only_after_changes(db_settings: Settings) -> None:
    store = FakeStore()
    first = offsite_backup(db_settings, store, "idle")
    assert first is not None and first.endswith("-idle.sqlite.gz")
    assert offsite_backup(db_settings, store, "idle") is None  # unchanged
    assert not list((db_settings.data_dir / "backups" / "offsite" / "new").iterdir())
    _write(
        db_settings, "INSERT INTO audit_log (at, action, detail) VALUES ('2026-01-01', 'x', '{}')"
    )
    assert offsite_backup(db_settings, store, "periodic") is not None
    local = db_settings.data_dir / "backups" / "offsite"
    assert len(list(local.glob("*.sqlite.gz"))) == 2
    assert json.loads((local / "last.json").read_text())["key"] in store.objects


def test_offsite_backup_without_a_database(tmp_path: Path) -> None:
    assert offsite_backup(Settings(data_dir=tmp_path), FakeStore(), "idle") is None


def test_prune_keeps_recent_and_one_per_week() -> None:
    now = datetime(2026, 11, 30, 12, tzinfo=UTC)

    def b(days: float) -> RemoteBackup:
        t = now - timedelta(days=days)
        return RemoteBackup(t.strftime("%Y%m%dT%H%M%SZ") + "-x.sqlite.gz", t, 1)

    recent = [b(0.1), b(1), b(13)]
    week45 = [b(26), b(23)]  # Wed 4 and Sat 7 November
    week46 = [b(19), b(16)]  # Wed 11 and Sat 14 November
    doomed = prune_plan(recent + week45 + week46, now, keep_days=14)
    assert sorted(doomed) == sorted([week45[0].key, week46[0].key])  # newest of each week stays


def test_store_needs_keys_when_a_bucket_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUCKET_NAME", "vfgh-bench-backups")  # as Tigris sets it on fly.io
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    s = Settings(data_dir=tmp_path, secrets_dir=tmp_path / "none")
    assert s.backup_bucket == "vfgh-bench-backups"
    with pytest.raises(RuntimeError, match="AWS_ACCESS_KEY_ID"):
        store_from_settings(s)
    assert store_from_settings(Settings(data_dir=tmp_path, BENCH_BACKUP_BUCKET="")) is None


def test_restore_is_staged_and_swapped_in(db_settings: Settings, tmp_path: Path) -> None:
    store = FakeStore()
    key = offsite_backup(db_settings, store, "idle")
    assert key is not None
    _write(
        db_settings,
        "INSERT INTO audit_log (at, action, detail) VALUES ('2026-01-01', 'later', '{}')",
    )
    stage_restore(db_settings, key, store)
    db_path = Path(db_settings.db_url.removeprefix("sqlite:///"))
    assert pending_path(db_path).exists()
    assert apply_pending_restore(db_settings.db_url) == db_path
    assert apply_pending_restore(db_settings.db_url) is None  # nothing staged any more
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM audit_log").fetchone() == (0,)
    kept = list(db_path.parent.glob("bench.sqlite.before-restore-*"))
    assert any(p.name.endswith("Z") for p in kept)  # the old database is kept


def test_restore_refuses_a_foreign_file(db_settings: Settings, tmp_path: Path) -> None:
    other = tmp_path / "other.sqlite"
    with sqlite3.connect(other) as db:
        db.execute("CREATE TABLE t (x)")
    with pytest.raises(ValueError, match="not a bench database"):
        stage_restore(db_settings, str(other), None)
    truncated = tmp_path / "truncated.sqlite.gz"
    good, _ = make_backup(db_settings.db_url, tmp_path / "b")
    truncated.write_bytes(good.read_bytes()[:200])
    with pytest.raises(EOFError):
        stage_restore(db_settings, str(truncated), None)
    junk = tmp_path / "junk.sqlite.gz"
    junk.write_bytes(gzip.compress(b"not a database at all" * 100))
    with pytest.raises(sqlite3.DatabaseError):
        stage_restore(db_settings, str(junk), None)
    with pytest.raises(FileNotFoundError):
        stage_restore(db_settings, "backups/missing.sqlite.gz", None)
    db_path = Path(db_settings.db_url.removeprefix("sqlite:///"))
    assert not pending_path(db_path).exists()  # nothing unchecked is ever staged
    assert not [p for p in db_path.parent.iterdir() if "restore" in p.name]  # no leftovers


def test_pending_migration(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'bench.sqlite'}")
    assert not pending_migration(engine)  # empty: nothing to back up
    migrate(engine)
    assert not pending_migration(engine)
    command.downgrade(alembic_config(engine), "-1")
    assert pending_migration(engine)


def test_the_bucket_can_be_set_by_field_name(tmp_path: Path) -> None:
    assert Settings(data_dir=tmp_path, backup_bucket="b").backup_bucket == "b"
