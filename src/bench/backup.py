"""Backups of the SQLite database: local, off-site to an S3 bucket, and restore.

`bench backup` writes a gzipped online backup to a directory; scheduling is the host's job.
`bench all` (fly.io) also uploads one to the bucket in ``BENCH_BACKUP_BUCKET`` before a
migration, every few hours while it runs and when it stops, each time only if the database
changed since the last upload. Old uploads are pruned: all from the last
``backup_keep_days`` days, before that the newest of each ISO week.

A restore is staged next to the database and swapped in by the next `bench all` start,
because the running processes hold the file open (`bench restore --now` swaps at once
when nothing runs).
"""

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from bench.settings import BACKUP_ACCESS_KEY_ID, BACKUP_SECRET_ACCESS_KEY, Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

log = logging.getLogger(__name__)

STAMP = "%Y%m%dT%H%M%SZ"
SUFFIX = ".sqlite.gz"
LOCAL_KEEP = 3  # off-site backups also kept on the volume, newest first


def _sqlite_path(db_url: str) -> Path:
    if not db_url.startswith("sqlite:///"):
        raise ValueError("backups only handle SQLite; use the database's own tools otherwise")
    return Path(db_url.removeprefix("sqlite:///"))


def make_backup(db_url: str, target_dir: Path, label: str | None = None) -> tuple[Path, str]:
    """Gzipped online backup; returns its path and the sha256 of the uncompressed copy.

    The copy of an unchanged database is byte-identical, so equal hashes mean no change.
    """
    source = _sqlite_path(db_url)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime(STAMP)
    name = f"{stamp}-{label}" if label else stamp
    raw = target_dir / f"{name}.sqlite"
    with sqlite3.connect(source) as src, sqlite3.connect(raw) as dst:
        src.backup(dst)  # consistent copy while web and worker keep running
    digest = hashlib.sha256()
    packed = raw.with_suffix(SUFFIX)
    with raw.open("rb") as f_in, gzip.open(packed, "wb") as f_out:
        while chunk := f_in.read(1 << 20):
            digest.update(chunk)
            f_out.write(chunk)
    raw.unlink()
    return packed, digest.hexdigest()


def backup_sqlite(db_url: str, target_dir: Path, label: str | None = None) -> Path:
    return make_backup(db_url, target_dir, label)[0]


# --- off-site ------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteBackup:
    key: str
    taken: datetime
    size: int


class Store(Protocol):
    def upload(self, path: Path) -> str: ...
    def list(self) -> list[RemoteBackup]: ...
    def delete(self, keys: Iterable[str]) -> None: ...
    def download(self, key: str, dest: Path) -> None: ...


def taken_at(key: str) -> datetime | None:
    """Time of a backup from its name (``<stamp>[-label].sqlite.gz``)."""
    name = key.rsplit("/", 1)[-1]
    try:
        return datetime.strptime(name[:16], STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


class S3Store:
    """Backups in an S3 bucket below a prefix. Endpoint and region come from the usual
    AWS variables (``AWS_ENDPOINT_URL_S3``, ``AWS_REGION``; Tigris sets both)."""

    def __init__(self, bucket: str, prefix: str, access_key: str, secret_key: str) -> None:
        import boto3
        from botocore.config import Config

        self.bucket, self.prefix = bucket, prefix
        self.client: S3Client = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            # checksums only where S3 requires them; not every S3-compatible store has
            # the newer default ones
            config=Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def upload(self, path: Path) -> str:
        key = self.prefix + path.name
        self.client.upload_file(str(path), self.bucket, key)
        return key

    def list(self) -> list[RemoteBackup]:
        found = []
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self.prefix
        )
        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(SUFFIX) and (taken := taken_at(key)):
                    found.append(RemoteBackup(key, taken, obj["Size"]))
        return sorted(found, key=lambda b: b.taken)

    def delete(self, keys: Iterable[str]) -> None:
        batch: list[ObjectIdentifierTypeDef] = [{"Key": k} for k in keys]
        for i in range(0, len(batch), 1000):
            self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": batch[i : i + 1000], "Quiet": True}
            )

    def download(self, key: str, dest: Path) -> None:
        self.client.download_file(self.bucket, key, str(dest))


def store_from_settings(settings: Settings) -> S3Store | None:
    if not settings.backup_bucket:
        return None
    access = settings.secret(BACKUP_ACCESS_KEY_ID)
    secret = settings.secret(BACKUP_SECRET_ACCESS_KEY)
    if not access or not secret:
        raise RuntimeError(
            f"backup bucket {settings.backup_bucket!r} set, but {BACKUP_ACCESS_KEY_ID} or "
            f"{BACKUP_SECRET_ACCESS_KEY} is missing"
        )
    return S3Store(settings.backup_bucket, settings.backup_prefix, access, secret)


def prune_plan(backups: Iterable[RemoteBackup], now: datetime, keep_days: int) -> list[str]:
    """Keys to delete: keep everything newer than ``keep_days``, before that the newest
    backup of each ISO week."""
    cutoff = now - timedelta(days=keep_days)
    newest_of_week: dict[tuple[int, int], RemoteBackup] = {}
    old = [b for b in backups if b.taken < cutoff]
    for b in old:
        week = b.taken.isocalendar()[:2]
        if week not in newest_of_week or b.taken > newest_of_week[week].taken:
            newest_of_week[week] = b
    keep = {b.key for b in newest_of_week.values()}
    return [b.key for b in old if b.key not in keep]


def _file_state(db_path: Path) -> list[list[int]]:
    """Size and mtime of the database and its WAL: unchanged files need no new copy."""
    state = []
    for suffix in ("", "-wal"):
        try:
            st = db_path.with_name(db_path.name + suffix).stat()
            state.append([st.st_size, st.st_mtime_ns])
        except OSError:
            state.append([])
    return state


def offsite_backup(settings: Settings, store: Store, label: str) -> str | None:
    """Upload a backup if the database changed since the last upload; returns its key."""
    db_path = _sqlite_path(settings.db_url)
    if not db_path.exists():
        return None
    local_dir = settings.data_dir / "backups" / "offsite"
    state_file = local_dir / "last.json"
    try:
        last = json.loads(state_file.read_text())
    except (OSError, ValueError):
        last = {}
    files = _file_state(db_path)  # before the copy: a write during it counts next time
    if last.get("files") == files:
        log.info("backup skipped: database files unchanged since %s", last.get("key"))
        return None
    # made aside first: an unchanged backup is dropped and must not replace a kept one
    # of the same second
    new, digest = make_backup(settings.db_url, local_dir / "new", label)
    if last.get("sha256") == digest:  # files touched, content the same (e.g. checkpoint)
        new.unlink()
        state_file.write_text(json.dumps({**last, "files": files}))
        log.info("backup skipped: database unchanged since %s", last.get("key"))
        return None
    path = new.rename(local_dir / new.name)
    key = store.upload(path)
    state_file.write_text(json.dumps({"sha256": digest, "key": key, "files": files}))
    log.info("backup uploaded: %s", key)
    for old in sorted(local_dir.glob("*" + SUFFIX), reverse=True)[LOCAL_KEEP:]:
        old.unlink()
    try:
        doomed = prune_plan(store.list(), datetime.now(UTC), settings.backup_keep_days)
        if doomed:
            store.delete(doomed)
            log.info("pruned %s old backups", len(doomed))
    except Exception:  # pruning may fail without losing the backup just made
        log.exception("pruning old backups failed")
    return key


# --- restore -------------------------------------------------------------------------


def pending_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".restore-pending")


def stage_restore(settings: Settings, source: str, store: Store | None) -> Path:
    """Fetch a backup (local file or bucket key), check it and stage it for the next start.

    Only a checked file ever reaches the pending path, so a broken download is never
    swapped in. Run as root (``fly ssh console``), the file is given to the owner of the
    data directory, the user the app runs as.
    """
    db_path = _sqlite_path(settings.db_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fetched = db_path.with_name(db_path.name + ".restore-download")
    checking = db_path.with_name(db_path.name + ".restore-check")
    try:
        local = Path(source)
        if local.is_file():
            shutil.copyfile(local, fetched)
        elif store is not None:
            store.download(source, fetched)
        else:
            raise FileNotFoundError(f"{source}: no such file and no backup bucket configured")
        with fetched.open("rb") as f:
            packed = f.read(2) == b"\x1f\x8b"
        if packed:
            with gzip.open(fetched, "rb") as f_in, checking.open("wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        else:
            shutil.copyfile(fetched, checking)
        with sqlite3.connect(checking) as db:
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError(f"{source}: integrity check failed")
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).fetchone():
                raise ValueError(f"{source}: not a bench database")
        if os.geteuid() == 0:
            owner = db_path.parent.stat()
            os.chown(checking, owner.st_uid, owner.st_gid)
        staged = pending_path(db_path)
        checking.rename(staged)
        return staged
    finally:
        fetched.unlink(missing_ok=True)
        checking.unlink(missing_ok=True)


def apply_pending_restore(db_url: str) -> Path | None:
    """Swap a staged restore in; the old database is kept beside it. Nothing may run."""
    db_path = _sqlite_path(db_url)
    staged = pending_path(db_path)
    if not staged.exists():
        return None
    stamp = datetime.now(UTC).strftime(STAMP)
    for suffix in ("", "-wal", "-shm"):
        old = db_path.with_name(db_path.name + suffix)
        if old.exists():
            old.rename(db_path.with_name(f"{db_path.name}.before-restore-{stamp}{suffix}"))
    staged.rename(db_path)
    log.warning("database restored from a staged backup; the old one is kept beside it")
    return db_path
