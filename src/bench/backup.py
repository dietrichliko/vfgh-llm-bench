"""Online backup of the SQLite database to a directory. Scheduling is left to the host."""

import gzip
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup_sqlite(db_url: str, target_dir: Path, label: str | None = None) -> Path:
    if not db_url.startswith("sqlite:///"):
        raise ValueError("bench backup only handles SQLite; use the database's own tools otherwise")
    source = Path(db_url.removeprefix("sqlite:///"))
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{label}" if label else stamp
    raw = target_dir / f"{name}.sqlite"
    with sqlite3.connect(source) as src, sqlite3.connect(raw) as dst:
        src.backup(dst)  # consistent copy while web and worker keep running
    packed = raw.with_suffix(".sqlite.gz")
    with raw.open("rb") as f_in, gzip.open(packed, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw.unlink()
    return packed
