"""`bench all`: idle stop, crashes, backups around the run, restore at start, activity."""

import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bench.backup import RemoteBackup, pending_path
from bench.db import make_engine, migrate
from bench.settings import Settings
from bench.supervisor import Supervisor
from bench.web.app import create_app

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


class RecordingStore:
    def __init__(self) -> None:
        self.uploaded: list[str] = []

    def upload(self, path: Path) -> str:
        self.uploaded.append(path.name)
        return path.name

    def list(self) -> list[RemoteBackup]:
        return []

    def delete(self, keys: Iterable[str]) -> None:
        pass

    def download(self, key: str, dest: Path) -> None:
        raise AssertionError("not used")


def jumping_clock(step: float) -> Callable[[], float]:
    """Each call is ``step`` seconds later than the one before."""
    start = time.time()
    calls = iter(range(10**6))
    return lambda: start + step * next(calls)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", idle_stop_minutes=15, backup_interval_hours=6)
    migrate(make_engine(s.db_url))
    return s


def test_idle_stop_ends_the_children_and_backs_up(settings: Settings) -> None:
    store = RecordingStore()
    sup = Supervisor(
        settings, {"a": SLEEP, "b": SLEEP}, store, poll_seconds=0.01, clock=jumping_clock(600)
    )
    assert sup.run() == 0  # fly.io leaves the machine stopped
    assert all(c.poll() is not None for c in sup.children.values())
    assert [n.split("-", 1)[1] for n in store.uploaded] == ["idle.sqlite.gz"]


def test_activity_postpones_the_idle_stop(settings: Settings) -> None:
    sup = Supervisor(settings, {}, None)
    now = time.time()
    assert sup.idle(now + 16 * 60, started=now, last_busy=now)
    assert not sup.idle(now + 14 * 60, started=now, last_busy=now)
    assert not sup.idle(now + 16 * 60, started=now, last_busy=now + 2 * 60)  # open jobs
    sup.marker.touch()  # a request
    assert not sup.idle(time.time() + 14 * 60, started=now, last_busy=now)
    never = Supervisor(settings.model_copy(update={"idle_stop_minutes": 0}), {}, None)
    assert not never.idle(now + 10**6, started=now, last_busy=now)


def test_a_dead_child_stops_the_machine_with_an_error(settings: Settings) -> None:
    store = RecordingStore()
    crash = [sys.executable, "-c", "raise SystemExit(3)"]
    sup = Supervisor(
        settings.model_copy(update={"idle_stop_minutes": 0}),
        {"web": SLEEP, "worker": crash},
        store,
        poll_seconds=0.05,
    )
    assert sup.run() == 1  # fly.io restarts the machine
    assert sup.children["web"].poll() is not None
    assert [n.split("-", 1)[1] for n in store.uploaded] == ["crash.sqlite.gz"]


def test_sigterm_stops_cleanly(settings: Settings) -> None:
    sup = Supervisor(
        settings.model_copy(update={"idle_stop_minutes": 0}),
        {"web": SLEEP},
        RecordingStore(),
        poll_seconds=0.05,
    )
    threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()
    assert sup.run() == 0
    assert signal.getsignal(signal.SIGTERM) is not sup._on_signal  # handler restored


def test_periodic_backup_and_no_duplicate_after_it(settings: Settings) -> None:
    store = RecordingStore()
    # 7 h per call: the first poll is past 6 h (backup), then idle; the database does not
    # change in between, so the stop makes no second upload
    sup = Supervisor(settings, {}, store, poll_seconds=0.01, clock=jumping_clock(7 * 3600))
    assert sup.run() == 0
    assert [n.split("-", 1)[1] for n in store.uploaded] == ["periodic.sqlite.gz"]


def test_start_swaps_in_a_staged_restore(settings: Settings, tmp_path: Path) -> None:
    db_path = Path(settings.db_url.removeprefix("sqlite:///"))
    other = Settings(data_dir=tmp_path / "other")
    migrate(make_engine(other.db_url))
    pending_path(db_path).write_bytes(Path(other.db_url.removeprefix("sqlite:///")).read_bytes())
    sup = Supervisor(settings, {}, None)
    sup.prepare(make_engine(settings.db_url))
    assert not pending_path(db_path).exists()
    assert list(db_path.parent.glob("bench.sqlite.before-restore-*"))


def test_web_marks_activity_but_not_health_checks(settings: Settings, tmp_path: Path) -> None:
    marker = tmp_path / "activity"
    s = settings.model_copy(update={"activity_file": marker, "dev_login": True})
    with TestClient(create_app(s)) as client:
        client.get("/health")
        assert not marker.exists()
        client.get("/login")
        assert marker.exists()
