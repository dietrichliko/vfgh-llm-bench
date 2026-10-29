"""Execute one task (e.g. a decision) on one model and classify the result.

Status:
- ``done``: an answer that validates against the output schema
- ``not_evaluable``: an answer arrived but does not fit the schema (never re-asked)
- ``failed``: no answer (HTTP error, connection, timeout)
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior

from bench.llm.http import Exchange, capture
from bench.llm.ping import reported_model_matches, usage_dict
from bench.models import JobStatus
from bench.providers import ResolvedModel, build_model, output_spec
from bench.settings import Settings


@dataclass
class TaskResult:
    status: JobStatus
    output: BaseModel | None
    error: str | None
    duration_ms: int
    response_text: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    cost_reported_usd: float | None = None
    model_reported: str | None = None
    provider_reported: str | None = None
    bridge_version: str | None = None
    model_mismatch: bool = False
    exchanges: list[Exchange] = field(default_factory=list)


def _last_success(exchanges: list[Exchange]) -> tuple[dict[str, Any], dict[str, str]]:
    for ex in reversed(exchanges):
        if ex.status == 200 and ex.response_body:
            try:
                data = json.loads(ex.response_body)
            except ValueError:
                return {}, ex.response_headers
            return (data if isinstance(data, dict) else {}), ex.response_headers
    return {}, {}


def _response_text(raw: dict[str, Any]) -> str | None:
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def cost(rm: ResolvedModel, usage: dict[str, Any]) -> float | None:
    price = rm.config.price
    if price is None:
        return None
    tokens_in = int(usage.get("input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    return (tokens_in * price.input + tokens_out * price.output) / 1_000_000


async def run_task[T: BaseModel](
    rm: ResolvedModel,
    settings: Settings,
    http_client: httpx2.AsyncClient,
    messages: list[dict[str, str]],
    output_type: type[T],
) -> TaskResult:
    system = next(m["content"] for m in messages if m["role"] == "system")
    user = next(m["content"] for m in messages if m["role"] == "user")
    spec = output_spec(rm, output_type)

    t0 = time.monotonic()
    status, output, error, usage = JobStatus.DONE, None, None, {}
    with capture() as cap:
        agent = Agent(
            build_model(rm, settings, http_client),
            output_type=spec,
            system_prompt=system,
            retries=0,  # never re-ask the model
        )
        try:
            result = await agent.run(user)
            output = result.output
            usage = usage_dict(result.usage)
        except UnexpectedModelBehavior as exc:
            status, error = JobStatus.NOT_EVALUABLE, f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            status, error = JobStatus.FAILED, f"{type(exc).__name__}: {exc}"

    raw, headers = _last_success(cap.exchanges)
    if status is not JobStatus.FAILED and not raw:
        status = JobStatus.FAILED
    if not usage and isinstance(raw.get("usage"), dict):
        u = raw["usage"]
        usage = {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
        }
    reported_cost = raw.get("usage", {}).get("cost") if isinstance(raw.get("usage"), dict) else None
    reported = raw.get("model") if isinstance(raw.get("model"), str) else None
    return TaskResult(
        status=status,
        output=output,
        error=error,
        duration_ms=round((time.monotonic() - t0) * 1000),
        response_text=_response_text(raw),
        usage=usage,
        cost_usd=cost(rm, usage),
        cost_reported_usd=float(reported_cost) if isinstance(reported_cost, int | float) else None,
        model_reported=reported,
        provider_reported=raw.get("provider") if isinstance(raw.get("provider"), str) else None,
        bridge_version=headers.get("x-bridge-version"),
        model_mismatch=reported is not None
        and not reported_model_matches(rm.config.model, reported),
        exchanges=cap.exchanges,
    )
