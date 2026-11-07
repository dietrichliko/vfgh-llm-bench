import httpx2
import pytest

from bench.llm.http import capture, make_http_client


async def _post(client: httpx2.AsyncClient) -> httpx2.Response:
    return await client.post(
        "http://llm.test/v1/chat/completions",
        json={"model": "m"},
        headers={"Authorization": "Bearer top-secret", "X-Bridge-Raw": "1"},
    )


async def test_transient_error_is_retried_and_every_attempt_recorded() -> None:
    statuses = iter([503, 429, 200])

    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(next(statuses), json={"n": 1}, headers={"X-Bridge-Version": "0.4.0"})

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        with capture() as cap:
            response = await _post(client)
    assert response.status_code == 200
    assert response.json() == {"n": 1}
    assert [ex.status for ex in cap.exchanges] == [503, 429, 200]
    assert [ex.attempt for ex in cap.exchanges] == [1, 2, 3]
    assert cap.exchanges[-1].response_headers["x-bridge-version"] == "0.4.0"
    assert cap.exchanges[0].request_body == '{"model":"m"}'


async def test_permanent_error_is_not_retried() -> None:
    calls = 0

    def handler(_req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(400, json={"error": "bad"})

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        response = await _post(client)
    assert response.status_code == 400
    assert calls == 1


async def test_retries_are_limited() -> None:
    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        with capture() as cap:
            response = await _post(client)
    assert response.status_code == 503
    assert len(cap.exchanges) == 3


async def test_timeout_is_never_retried() -> None:
    calls = 0

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        raise httpx2.ReadTimeout("slow", request=req)

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        with capture() as cap, pytest.raises(httpx2.ReadTimeout):
            await _post(client)
    assert calls == 1
    assert cap.exchanges[0].error is not None and cap.exchanges[0].error.startswith("timeout")


async def test_connection_error_is_retried() -> None:
    calls = 0

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx2.ConnectError("refused", request=req)
        return httpx2.Response(200, json={})

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        response = await _post(client)
    assert response.status_code == 200
    assert calls == 2


async def test_credentials_are_never_recorded() -> None:
    async with make_http_client(
        httpx2.MockTransport(lambda _r: httpx2.Response(200)), backoff=0
    ) as client:
        with capture() as cap:
            await _post(client)
    headers = cap.exchanges[0].request_headers
    assert "authorization" not in {k.lower() for k in headers}
    assert headers["x-bridge-raw"] == "1"
    assert "top-secret" not in str(cap.exchanges[0].as_dict())


async def test_nothing_recorded_outside_capture() -> None:
    async with make_http_client(
        httpx2.MockTransport(lambda _r: httpx2.Response(200)), backoff=0
    ) as client:
        await _post(client)
        with capture() as cap:
            pass
    assert cap.exchanges == []


async def test_compressed_responses_are_decoded_once() -> None:
    """OpenRouter answers gzip-compressed; the body must reach the client decoded once."""
    import gzip
    import json

    body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()

    def handler(_req: httpx2.Request) -> httpx2.Response:
        packed = gzip.compress(body)
        return httpx2.Response(
            200,
            content=packed,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(packed)),
                "Content-Type": "application/json",
            },
        )

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        with capture() as cap:
            response = await _post(client)
    assert response.json() == json.loads(body)
    assert cap.exchanges[0].response_body == body.decode()  # recorded readable
    assert cap.exchanges[0].response_headers["content-encoding"] == "gzip"  # as received
