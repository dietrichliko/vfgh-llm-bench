"""Starting and stopping the u:ai bridge app on fly.io (Machines API mocked)."""

import sys
from pathlib import Path
from typing import Any

import httpx

from bench.db import make_engine, migrate
from bench.flymachines import FlyMachines, auth_header
from bench.settings import Settings
from bench.supervisor import Supervisor

API = "http://_api.internal:4280"
HEALTH = "http://vfgh-bench-bridge.internal:8000/health"


class FakeFly:
    def __init__(self, states: list[str], healthy_after: int = 0) -> None:
        self.machines = [{"id": f"m{i}", "state": s} for i, s in enumerate(states)]
        self.calls: list[str] = []
        self.health_calls = 0
        self.healthy_after = healthy_after
        self.health_auth: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "vfgh-bench-bridge.internal":
            self.health_calls += 1
            self.health_auth.append(request.headers.get("authorization"))
            ok = self.health_calls > self.healthy_after
            return httpx.Response(200 if ok else 503)
        assert request.headers["authorization"] == "FlyV1 fm2_secret"
        path = request.url.path.removeprefix("/v1/apps/vfgh-bench-bridge/machines")
        self.calls.append(f"{request.method} {path or '/'}")
        if request.method == "GET" and path == "":
            return httpx.Response(200, json=self.machines)
        mid, _, action = path.strip("/").partition("/")
        machine = next(m for m in self.machines if m["id"] == mid)
        if action == "start":
            machine["state"] = "started"
        elif action == "stop":
            machine["state"] = "stopped"
        return httpx.Response(200, json={"ok": True})


def machines(fake: FakeFly, health: str = HEALTH) -> FlyMachines:
    return FlyMachines(
        API,
        "FlyV1 fm2_secret",
        "vfgh-bench-bridge",
        health,
        transport=httpx.MockTransport(fake.handler),
        ready_timeout=1.0,
        sleep=0.01,
    )


def test_auth_header() -> None:
    assert auth_header("FlyV1 fm2_x") == "FlyV1 fm2_x"
    assert auth_header("abc") == "Bearer abc"


def test_start_waits_for_the_health_check_without_the_token() -> None:
    fake = FakeFly(["stopped", "destroyed"], healthy_after=2)
    assert machines(fake).start()
    assert fake.calls == ["GET /", "POST /m0/start", "GET /m0/wait"]
    assert fake.health_calls == 3
    assert fake.health_auth == [None, None, None]  # the fly token never reaches the bridge


def test_a_running_bridge_is_not_started_again_and_stop_stops_it() -> None:
    fake = FakeFly(["started"])
    fm = machines(fake, health="")
    assert fm.start()
    assert fake.calls == ["GET /", "GET /m0/wait"]
    fm.stop()
    assert fake.calls[-1] == "POST /m0/stop"
    assert fake.machines[0]["state"] == "stopped"


def test_failures_are_logged_not_raised() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    fm = FlyMachines(API, "t", "vfgh-bench-bridge", transport=httpx.MockTransport(down))
    assert not fm.start()
    fm.stop()  # no exception
    unhealthy = FakeFly(["stopped"], healthy_after=10**6)
    assert not machines(unhealthy).start()


def test_a_stopping_bridge_is_started_once_it_can_be() -> None:
    fake = FakeFly(["stopping"])
    refusals = iter([True, True, False])
    inner = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start") and next(refusals):
            return httpx.Response(412, json={"error": "machine still stopping"})
        return inner(request)

    fm = FlyMachines(
        API,
        "FlyV1 fm2_secret",
        "vfgh-bench-bridge",
        transport=httpx.MockTransport(handler),
        sleep=0.01,
    )
    assert fm.start()
    assert fake.machines[0]["state"] == "started"


def test_supervisor_order_web_bridge_worker(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path / "data", idle_stop_minutes=15)
    migrate(make_engine(s.db_url))
    order: list[str] = []

    class Bridge:
        def start(self) -> bool:
            order.append("bridge start")
            return True

        def stop(self) -> None:
            order.append("bridge stop")

    sleep = [sys.executable, "-c", "import time; time.sleep(60)"]
    sup = Supervisor(
        s,
        {"web": sleep, "worker": sleep},
        None,
        poll_seconds=0.01,
        clock=iter_clock(),
        bridge=Bridge(),
    )
    orig_start, orig_stop = sup.start, sup.stop

    def start(names: list[str]) -> None:
        order.append(f"start {','.join(names)}")
        orig_start(names)

    def stop() -> None:
        order.append("children stop")
        orig_stop()

    sup.start, sup.stop = start, stop  # type: ignore[method-assign]
    assert sup.run() == 0
    # web answers the waking request while the bridge boots; the worker comes after it
    assert order == ["start web", "bridge start", "start worker", "children stop", "bridge stop"]


def test_a_failing_supervisor_still_cleans_up(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path / "data")
    migrate(make_engine(s.db_url))
    stopped: list[str] = []

    class Bridge:
        def start(self) -> bool:
            raise RuntimeError("boom")

        def stop(self) -> None:
            stopped.append("bridge")

    sleep = [sys.executable, "-c", "import time; time.sleep(60)"]
    sup = Supervisor(s, {"web": sleep}, None, bridge=Bridge())
    assert sup.run() == 1
    assert stopped == ["bridge"]
    assert sup.children["web"].poll() is not None  # web was stopped too


def iter_clock() -> Any:
    import time

    t = iter(range(10**6))
    start = time.time()
    return lambda: start + 3600 * next(t)
