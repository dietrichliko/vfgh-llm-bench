import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from bench.llm.http import make_http_client
from bench.settings import Settings
from bench.web.app import create_app

os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

CONFIG = """
providers:
  uai:
    base_url: http://bridge/v1
    api_key_secret: TEST_UAI_KEY
    headers: {X-Bridge-Raw: "1"}
    output_mode: prompted
    tool_mode: emulated
  openrouter:
    base_url: https://openrouter.test/api/v1
    api_key_secret: TEST_OPENROUTER_KEY
    tool_mode: native
    extra_body: {provider: {allow_fallbacks: false}}
models:
  judge:
    provider: uai
    model: claude-opus-4-8
    family: anthropic
    roles: [judge]
  glm:
    provider: openrouter
    model: z-ai/glm-5.3
    family: zhipu
    cutoff: 2026-01-01
    roles: [tested]
"""


def chat_response(
    content: str = "OK", model: str = "claude-opus-4-8", **extra: Any
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
        **extra,
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("BENCH_") or name.startswith("TEST_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    d = tmp_path / "secrets"
    d.mkdir()
    (d / "TEST_UAI_KEY").write_text("uai-secret-value\n")
    return d


@pytest.fixture
def settings(tmp_path: Path, secrets_dir: Path) -> Settings:
    config = tmp_path / "providers.yaml"
    config.write_text(CONFIG)
    return Settings(
        data_dir=tmp_path / "data",
        BENCH_CONFIG=config,
        secrets_dir=secrets_dir,
        dev_login=True,
        admin_emails=["admin@example.org"],
    )


Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture
def llm() -> dict[str, Any]:
    """Mutable mock for the LLM endpoint: set ``llm["handler"]``; requests are collected."""
    state: dict[str, Any] = {
        "requests": [],
        "handler": lambda req: httpx2.Response(200, json=chat_response()),
    }
    return state


@pytest.fixture
def client(settings: Settings, llm: dict[str, Any]) -> Iterator[TestClient]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        llm["requests"].append(request)
        response: httpx2.Response = llm["handler"](request)
        return response

    http_client = make_http_client(httpx2.MockTransport(handler), backoff=0)
    with TestClient(create_app(settings, http_client=http_client)) as c:
        yield c


def login(client: TestClient, email: str, name: str = "") -> None:
    r = client.post("/auth/dev-login", data={"email": email, "name": name}, follow_redirects=False)
    assert r.status_code == 303
