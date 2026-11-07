"""`bench all`: web, worker and ris-mcp in one machine (fly.io).

fly.io volumes belong to one machine, so everything that opens the SQLite file has to run
there. The supervisor:

- swaps in a staged restore, backs up off-site if a migration is pending, migrates;
- starts web, worker and ris-mcp as child processes and exits 1 if one of them dies
  (fly.io restarts the machine);
- uploads a backup every ``backup_interval_hours`` (only after changes);
- stops with exit 0 after ``idle_stop_minutes`` without requests and without open jobs, so
  that fly.io leaves the machine stopped until the next request starts it. fly.io's own
  auto-stop cannot be used: it sees only incoming requests, not the worker's LLM calls;
- starts the u:ai bridge app (fly.io Machines API) before the children and stops it after
  them, see bench/flymachines.py;
- on SIGTERM/SIGINT (deploy, `fly machine stop`) stops the children likewise; both end with
  a last backup.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import Protocol

from sqlalchemy import Engine, and_, func, or_
from sqlmodel import Session, col, select

from bench.activity import ActivityMarker
from bench.backup import Store, apply_pending_restore, offsite_backup
from bench.db import make_engine, migrate, pending_migration
from bench.models.runs import Job, JobStatus, Order, OrderStatus
from bench.settings import Settings

log = logging.getLogger(__name__)


class Companion(Protocol):
    """Another app started and stopped with this one (FlyMachines: the u:ai bridge)."""

    def start(self) -> bool: ...
    def stop(self) -> None: ...


STOP_TIMEOUT = 30.0  # seconds a child gets to end after SIGTERM


def default_commands(port: int, mcp_port: int) -> dict[str, list[str]]:
    bench = [sys.executable, "-m", "bench.cli"]
    return {
        "web": [*bench, "serve", "--port", str(port)],
        "worker": [*bench, "worker"],
        "ris-mcp": [*bench, "ris-mcp", "--host", "127.0.0.1", "--port", str(mcp_port)],
    }


def has_open_jobs(engine: Engine) -> bool:
    """A job is running, or queued in a running order (paused orders do not count)."""
    with Session(engine) as db:
        count = db.exec(
            select(func.count())
            .select_from(Job)
            .join(Order, col(Job.order_id) == col(Order.id))
            .where(
                or_(
                    col(Job.status) == JobStatus.RUNNING,
                    and_(
                        col(Job.status) == JobStatus.QUEUED,
                        col(Order.status) == OrderStatus.RUNNING,
                    ),
                )
            )
        ).one()
    return count > 0


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        commands: dict[str, list[str]],
        store: Store | None,
        *,
        poll_seconds: float = 15.0,
        clock: Callable[[], float] = time.time,
        bridge: Companion | None = None,
    ) -> None:
        self.settings = settings
        self.bridge = bridge
        self.commands = commands
        self.store = store
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.idle_seconds = settings.idle_stop_minutes * 60.0
        self.activity = settings.activity_file or settings.data_dir / ".activity"
        self.marker = ActivityMarker(self.activity)
        self.stop_requested = threading.Event()
        self.children: dict[str, subprocess.Popen[bytes]] = {}

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s: stopping", signal.Signals(signum).name)
        self.stop_requested.set()

    def _backup(self, label: str) -> None:
        if self.store is None:
            return
        try:
            offsite_backup(self.settings, self.store, label)
        except Exception:  # a failed backup must not take the app down
            log.exception("off-site backup (%s) failed", label)

    def prepare(self, engine: Engine) -> None:
        if apply_pending_restore(self.settings.db_url):
            log.warning("restored database swapped in")
        if pending_migration(engine):
            self._backup("pre-migrate")
        migrate(engine)

    def start(self) -> None:
        env = {**os.environ, "BENCH_ACTIVITY_FILE": str(self.activity)}
        for name, cmd in self.commands.items():
            self.children[name] = subprocess.Popen(cmd, env=env)
            log.info("started %s (pid %s)", name, self.children[name].pid)

    def stop(self) -> None:
        for child in self.children.values():
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + STOP_TIMEOUT
        for name, child in self.children.items():
            try:
                child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning("%s did not stop in time: killed", name)
                child.kill()
                child.wait()

    def idle(self, now: float, started: float, last_busy: float) -> bool:
        if self.idle_seconds <= 0:
            return False
        last = max(started, last_busy, self.marker.last() or 0.0)
        return now - last >= self.idle_seconds

    def run(self) -> int:
        previous = {
            sig: signal.signal(sig, self._on_signal) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            return self._run()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _run(self) -> int:
        engine = make_engine(self.settings.db_url)
        self.prepare(engine)
        if self.bridge:
            self.bridge.start()  # before the worker sends its first u:ai call
        self.start()
        started = last_busy = last_backup = self.clock()
        interval = self.settings.backup_interval_hours * 3600.0
        code, reason = 0, "stop"
        while not self.stop_requested.wait(self.poll_seconds):
            dead = [n for n, c in self.children.items() if c.poll() is not None]
            if dead:
                log.error("%s exited: stopping the machine", ", ".join(dead))
                code, reason = 1, "crash"
                break
            now = self.clock()
            if has_open_jobs(engine):
                last_busy = now
            if interval > 0 and now - last_backup >= interval:
                self._backup("periodic")
                last_backup = now
            if self.idle(now, started, last_busy):
                log.info("idle for %s min: stopping", self.settings.idle_stop_minutes)
                reason = "idle"
                break
        self.stop()
        if self.bridge:
            self.bridge.stop()
        self._backup(reason)
        engine.dispose()
        return code
