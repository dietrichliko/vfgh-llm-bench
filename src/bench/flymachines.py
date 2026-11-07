"""Start and stop the machines of another fly.io app (the u:ai bridge) via the Machines API.

The worker reaches the bridge over fly.io's private network (``<app>.internal``), not
through the proxy: the proxy closes connections that carry no data for 60 s, and a
non-streaming LLM call is silent until the answer is complete. Without the proxy nothing
starts the bridge on demand, so `bench all` starts it with itself and stops it with itself.
Failures are logged, never raised: the app runs on, and u:ai calls fail visibly.
"""

import logging
import time

import httpx

log = logging.getLogger(__name__)

RUNNING = {"started", "starting"}
STOPPABLE = {"started", "starting"}


def auth_header(token: str) -> str:
    """Tokens from `fly tokens create` carry their scheme (``FlyV1 …``); others are Bearer."""
    return token if token.startswith("FlyV1 ") else f"Bearer {token}"


class FlyMachines:
    def __init__(
        self,
        api_url: str,
        token: str,
        app: str,
        health_url: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
        ready_timeout: float = 90.0,
        sleep: float = 2.0,
    ) -> None:
        self.app, self.health_url = app, health_url
        self.ready_timeout, self.sleep = ready_timeout, sleep
        self.client = httpx.Client(
            base_url=api_url.rstrip("/") + f"/v1/apps/{app}",
            headers={"Authorization": auth_header(token)},
            transport=transport,
            timeout=30.0,
        )
        # separate client: the fly token must not reach the bridge
        self.probe = httpx.Client(transport=transport, timeout=5.0)

    def _machines(self) -> list[dict[str, str]]:
        r = self.client.get("/machines")
        r.raise_for_status()
        return [m for m in r.json() if m.get("state") != "destroyed"]

    def start(self) -> bool:
        """Start all machines of the app and wait until they (and the health URL) are up."""
        try:
            machines = self._machines()
            if not machines:
                log.error("fly app %s has no machines to start", self.app)
                return False
            for m in machines:
                if m.get("state") not in RUNNING:
                    self._start_machine(m["id"])
                w = self.client.get(
                    f"/machines/{m['id']}/wait",
                    params={"state": "started", "timeout": 60},
                    timeout=70.0,
                )
                w.raise_for_status()
            return self._wait_healthy()
        except httpx.HTTPError as exc:
            log.error("could not start fly app %s: %s", self.app, exc)
            return False

    def _start_machine(self, mid: str) -> None:
        """Start; a machine still stopping refuses for a moment, so retry until it can."""
        deadline = time.monotonic() + self.ready_timeout
        while True:
            r = self.client.post(f"/machines/{mid}/start")
            if r.is_success:
                log.info("starting %s machine %s", self.app, mid)
                return
            if time.monotonic() >= deadline:
                r.raise_for_status()
            log.info("%s machine %s not startable yet (%s): retrying", self.app, mid, r.status_code)
            time.sleep(self.sleep)

    def _wait_healthy(self) -> bool:
        if not self.health_url:
            return True
        deadline = time.monotonic() + self.ready_timeout
        while True:
            try:
                if self.probe.get(self.health_url).status_code == 200:
                    log.info("%s is up", self.app)
                    return True
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                log.error("%s not healthy after %.0f s", self.app, self.ready_timeout)
                return False
            time.sleep(self.sleep)

    def stop(self) -> None:
        try:
            for m in self._machines():
                if m.get("state") in STOPPABLE:
                    self.client.post(f"/machines/{m['id']}/stop", json={}).raise_for_status()
                    log.info("stopped %s machine %s", self.app, m["id"])
        except httpx.HTTPError as exc:
            log.error("could not stop fly app %s: %s", self.app, exc)

    def close(self) -> None:
        self.client.close()
        self.probe.close()
