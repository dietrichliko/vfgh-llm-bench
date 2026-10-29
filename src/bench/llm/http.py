"""HTTP transport for all LLM calls: our only retry logic, and a record of every exchange.

The openai SDK runs with ``max_retries=0``; this transport retries only transient errors
(429/502/503/504, connection errors) on the same endpoint. A timeout is never retried,
because the request may already be running (and billed) at the provider.

Every attempt is recorded with the exact request body and the raw response, so a run can
show what was sent and received. Records go to the ``Capture`` that is active in the
current task (see ``capture()``). The Authorization header is never recorded.
"""

import asyncio
import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx2

TRANSIENT_STATUS = frozenset({429, 502, 503, 504})
MAX_ATTEMPTS = 3
_HIDDEN_HEADERS = frozenset({"authorization", "cookie", "x-api-key", "api-key"})


@dataclass
class Exchange:
    attempt: int
    method: str
    url: str
    request_headers: dict[str, str]
    request_body: str
    status: int | None
    response_headers: dict[str, str]
    response_body: str | None
    error: str | None
    duration_ms: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Capture:
    exchanges: list[Exchange] = field(default_factory=list)


_current: ContextVar[Capture | None] = ContextVar("bench_capture", default=None)


@contextlib.contextmanager
def capture() -> Iterator[Capture]:
    """Collect all exchanges made inside this block (in this task and its children)."""
    cap = Capture()
    token = _current.set(cap)
    try:
        yield cap
    finally:
        _current.reset(token)


def _visible(headers: httpx2.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HIDDEN_HEADERS}


class RecordingTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport | None = None,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        backoff: float = 5.0,
    ) -> None:
        self._inner = inner or httpx2.AsyncHTTPTransport()
        self._max_attempts = max_attempts
        self._backoff = backoff

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = (await request.aread()).decode("utf-8", "replace")
        for attempt in range(1, self._max_attempts + 1):
            t0 = time.monotonic()
            try:
                response = await self._inner.handle_async_request(request)
                content = await response.aread()
            except httpx2.TimeoutException as exc:
                self._record(request, body, attempt, t0, error=f"timeout: {exc}")
                raise
            except (httpx2.ConnectError, httpx2.RemoteProtocolError) as exc:
                self._record(request, body, attempt, t0, error=f"{type(exc).__name__}: {exc}")
                if attempt == self._max_attempts:
                    raise
            else:
                self._record(request, body, attempt, t0, response=response, content=content)
                if response.status_code not in TRANSIENT_STATUS or attempt == self._max_attempts:
                    return httpx2.Response(
                        response.status_code,
                        headers=response.headers,
                        content=content,
                        request=request,
                        extensions=response.extensions,
                    )
            await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    def _record(
        self,
        request: httpx2.Request,
        body: str,
        attempt: int,
        t0: float,
        *,
        response: httpx2.Response | None = None,
        content: bytes | None = None,
        error: str | None = None,
    ) -> None:
        cap = _current.get()
        if cap is None:
            return
        cap.exchanges.append(
            Exchange(
                attempt=attempt,
                method=request.method,
                url=str(request.url),
                request_headers=_visible(request.headers),
                request_body=body,
                status=response.status_code if response is not None else None,
                response_headers=_visible(response.headers) if response is not None else {},
                response_body=content.decode("utf-8", "replace") if content is not None else None,
                error=error,
                duration_ms=round((time.monotonic() - t0) * 1000),
            )
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def make_http_client(
    inner: httpx2.AsyncBaseTransport | None = None, **kwargs: Any
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=RecordingTransport(inner, **kwargs), timeout=None)
