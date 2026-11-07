import json

import httpx2
import pytest

from bench.llm.http import make_http_client
from bench.llm.ping import PING_PROMPT, ping, reported_model_matches
from bench.providers import parse_config, resolve
from bench.settings import Settings
from tests.conftest import CONFIG, chat_response


@pytest.fixture
def judge(settings: Settings):  # type: ignore[no-untyped-def]
    return resolve(parse_config(CONFIG), settings)["judge"]


async def test_ping_sends_exactly_the_configured_request(settings: Settings, judge) -> None:  # type: ignore[no-untyped-def]
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, json=chat_response(), headers={"X-Bridge-Version": "0.4.0"})

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        res = await ping(judge, settings, client)

    assert res.ok and res.answer == "OK"
    assert res.bridge_version == "0.4.0"
    assert res.usage == {"input_tokens": 7, "output_tokens": 1, "requests": 1}
    req = seen[0]
    assert str(req.url) == "http://bridge/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer uai-secret-value"
    assert req.headers["x-bridge-raw"] == "1"
    body = json.loads(req.content)
    # no invented parameters: no temperature, no max_tokens, nothing but model and prompt
    assert body == {
        "model": "claude-opus-4-8",
        "stream": False,
        "messages": [{"role": "user", "content": PING_PROMPT}],
    }


async def test_ping_flags_a_different_model(settings: Settings, judge) -> None:  # type: ignore[no-untyped-def]
    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=chat_response(model="claude-opus-4-9"))

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        res = await ping(judge, settings, client)
    assert res.ok
    assert res.model_reported == "claude-opus-4-9"
    assert res.model_mismatch


async def test_ping_reports_errors_with_raw_exchange(settings: Settings, judge) -> None:  # type: ignore[no-untyped-def]
    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": {"message": "invalid key"}})

    async with make_http_client(httpx2.MockTransport(handler), backoff=0) as client:
        res = await ping(judge, settings, client)
    assert not res.ok
    assert res.error is not None
    assert len(res.exchanges) == 1
    assert res.exchanges[0].status == 401
    assert "invalid key" in (res.exchanges[0].response_body or "")


@pytest.mark.parametrize(
    ("requested", "reported", "ok"),
    [
        ("gpt-5.5", "gpt-5.5", True),
        ("gpt-5.5", "gpt-5.5-2026-08-01", True),
        ("z-ai/glm-5.3", "z-ai/glm-5.3", True),
        ("z-ai/glm-5.2", "z-ai/glm-5.3", False),
        ("claude-opus-4-8", "claude-opus-4-9", False),
    ],
)
def test_reported_model_matches(requested: str, reported: str, ok: bool) -> None:
    assert reported_model_matches(requested, reported) is ok


def test_usage_with_a_decimal_cost_can_be_stored_as_json() -> None:
    """Pydantic AI estimates a cost as Decimal for models it knows (the OpenRouter ones)."""
    import json
    from decimal import Decimal

    from pydantic_ai import RunUsage

    from bench.llm.ping import usage_dict

    usage = RunUsage(input_tokens=10, output_tokens=5)
    usage.cost = Decimal("0.0123")
    data = usage_dict(usage)
    assert json.loads(json.dumps(data)) == {"input_tokens": 10, "output_tokens": 5, "cost": 0.0123}
