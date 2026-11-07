"""Short test call to one model (admin page, ``bench check-providers``)."""

import dataclasses
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx2
from pydantic_ai import Agent, RunUsage

from bench.llm.http import Exchange, capture
from bench.providers import ResolvedModel, build_model
from bench.settings import Settings

PING_PROMPT = "Antworte nur mit dem Wort OK."


@dataclass
class PingResult:
    ok: bool
    answer: str | None
    error: str | None
    duration_ms: int
    model_reported: str | None = None
    provider_reported: str | None = None
    bridge_version: str | None = None
    model_mismatch: bool = False  # the response names a different model than requested
    usage: dict[str, Any] = field(default_factory=dict)
    exchanges: list[Exchange] = field(default_factory=list)


def _last_json(exchanges: list[Exchange]) -> dict[str, Any]:
    for ex in reversed(exchanges):
        if ex.response_body:
            try:
                data = json.loads(ex.response_body)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return {}


def _plain(value: Any) -> Any:
    """Decimal (Pydantic AI's own cost estimate) as float, so the dict can be stored as JSON."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def usage_dict(usage: RunUsage) -> dict[str, Any]:
    """Non-zero counters only; JSON-serialisable."""
    data = dataclasses.asdict(usage)
    return {k: _plain(v) for k, v in data.items() if v not in (0, None, {}, [])}


def reported_model_matches(requested: str, reported: str) -> bool:
    """OpenAI-style APIs may append a date or a provider prefix; anything else is a mismatch."""
    req, rep = requested.lower(), reported.lower()
    return rep == req or rep.startswith(req + "-") or rep.endswith("/" + req.split("/")[-1])


async def ping(
    rm: ResolvedModel, settings: Settings, http_client: httpx2.AsyncClient
) -> PingResult:
    t0 = time.monotonic()
    with capture() as cap:
        try:
            agent = Agent(build_model(rm, settings, http_client), retries=0)
            result = await agent.run(PING_PROMPT)
        except Exception as exc:
            return PingResult(
                ok=False,
                answer=None,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=round((time.monotonic() - t0) * 1000),
                exchanges=cap.exchanges,
            )
    raw = _last_json(cap.exchanges)
    headers = cap.exchanges[-1].response_headers if cap.exchanges else {}
    reported = raw.get("model") if isinstance(raw.get("model"), str) else None
    return PingResult(
        ok=True,
        answer=str(result.output),
        error=None,
        duration_ms=round((time.monotonic() - t0) * 1000),
        model_reported=reported,
        provider_reported=raw.get("provider") if isinstance(raw.get("provider"), str) else None,
        bridge_version=headers.get("x-bridge-version"),
        model_mismatch=reported is not None
        and not reported_model_matches(rm.config.model, reported),
        usage=usage_dict(result.usage),
        exchanges=cap.exchanges,
    )
