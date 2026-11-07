"""Execute one task (e.g. a decision) on one model and classify the result.

Status:
- ``done``: an answer that validates against the output schema
- ``not_evaluable``: an answer arrived but does not fit the schema (never re-asked)
- ``failed``: no answer (HTTP error, connection, timeout), or not the expected tool set

With a toolset (condition C) the model may call tools up to ``TOOL_CALL_LIMIT`` times.
Tool errors go back to the model as failed results; exceeding the limit makes the run
``not_evaluable``. Every call is logged with its arguments and result.
"""

import asyncio
import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2
from pydantic import BaseModel
from pydantic_ai import Agent, UsageLimitExceeded, UsageLimits, capture_run_messages
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from bench.llm.http import Exchange, capture
from bench.llm.ping import reported_model_matches, usage_dict
from bench.models import JobStatus
from bench.providers import ResolvedModel, build_model, max_output_tokens, output_spec
from bench.settings import Settings

TOOL_CALL_LIMIT = 15


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
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tools_sha256: str | None = None


def tool_log(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    """Tool calls in order, each with the result or error that went back to the model."""
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        for part in message.parts:
            if isinstance(message, ModelResponse) and isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = {
                    "id": part.tool_call_id,
                    "name": part.tool_name,
                    "args": part.args_as_dict(),
                    "result": None,
                    "error": False,
                }
            elif isinstance(message, ModelRequest) and isinstance(
                part, ToolReturnPart | RetryPromptPart
            ):
                call = calls.get(part.tool_call_id or "")
                if call is None:
                    continue
                content = part.content
                call["result"] = (
                    content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False, default=str)
                )
                call["error"] = isinstance(part, RetryPromptPart) or (
                    getattr(part, "outcome", "success") != "success"
                )
                call["at"] = part.timestamp.isoformat()
    return list(calls.values())


def tools_digest(tools: list[Any]) -> str:
    """sha256 over the tool definitions as offered (name, description, input schema)."""
    data = [
        {"name": t.name, "description": t.description, "schema": t.input_schema}
        for t in sorted(tools, key=lambda t: t.name)
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _last_success(exchanges: list[Exchange]) -> tuple[dict[str, Any], dict[str, str]]:
    for ex in reversed(exchanges):
        if ex.status == 200 and ex.response_body:
            try:
                data = json.loads(ex.response_body)
            except ValueError:
                return {}, ex.response_headers
            return (data if isinstance(data, dict) else {}), ex.response_headers
    return {}, {}


def _finish_reason(raw: dict[str, Any]) -> str | None:
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        return reason if isinstance(reason, str) else None
    return None


def _raw_usage(exchanges: list[Exchange]) -> dict[str, Any]:
    """Usage summed over all answered requests (a tool run has several), as reported."""
    total: dict[str, Any] = {}
    for ex in exchanges:
        if ex.status != 200 or not ex.response_body:
            continue
        try:
            u = json.loads(ex.response_body).get("usage")
        except (ValueError, AttributeError):
            continue
        if not isinstance(u, dict):
            continue
        total["input_tokens"] = total.get("input_tokens", 0) + int(u.get("prompt_tokens") or 0)
        total["output_tokens"] = total.get("output_tokens", 0) + int(
            u.get("completion_tokens") or 0
        )
        if isinstance(u.get("cost"), int | float):
            total["cost"] = total.get("cost", 0.0) + float(u["cost"])
    return total


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
    toolset: MCPToolset[Any] | None = None,
    expected_tools: frozenset[str] | None = None,
) -> TaskResult:
    system = next(m["content"] for m in messages if m["role"] == "system")
    user = next(m["content"] for m in messages if m["role"] == "user")
    spec = output_spec(rm, output_type)

    t0 = time.monotonic()
    status, output, error, usage = JobStatus.DONE, None, None, {}
    tools_sha256 = None
    async with contextlib.AsyncExitStack() as stack:
        cap = stack.enter_context(capture())
        messages_seen = stack.enter_context(capture_run_messages())
        try:
            tools = None
            if toolset is not None:
                await stack.enter_async_context(toolset)
                offered = await toolset.list_tools()
                tools_sha256 = tools_digest(offered)
                names = {t.name for t in offered}
                if expected_tools is not None and names != expected_tools:
                    raise RuntimeError(f"unerwartete Werkzeuge: {sorted(names)}")
                tools = [toolset]
            agent = Agent(
                build_model(rm, settings, http_client),
                output_type=spec,
                system_prompt=system,
                retries=0,  # never re-ask the model
                toolsets=tools,
            )
            limits = UsageLimits(tool_calls_limit=TOOL_CALL_LIMIT) if tools else None
            async with asyncio.timeout(rm.provider.max_run_seconds):
                result = await agent.run(user, usage_limits=limits)
            output = result.output
            usage = usage_dict(result.usage)
        except TimeoutError:
            # also when the endpoint keeps the connection alive without an answer
            status, error = (
                JobStatus.FAILED,
                f"Zeitlimit von {rm.provider.max_run_seconds:g} s für den Lauf überschritten",
            )
        except UsageLimitExceeded as exc:
            status, error = JobStatus.NOT_EVALUABLE, f"Werkzeuglimit überschritten: {exc}"
        except UnexpectedModelBehavior as exc:
            status, error = JobStatus.NOT_EVALUABLE, f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            status, error = JobStatus.FAILED, f"{type(exc).__name__}: {exc}"

    raw, headers = _last_success(cap.exchanges)
    if status is not JobStatus.FAILED and not raw:
        status = JobStatus.FAILED
    if status is not JobStatus.DONE and _finish_reason(raw) == "length":
        error = (
            f"Antwort bei max_tokens ({max_output_tokens(rm)}) abgeschnitten"
            f" ({error or 'nicht auswertbar'})"
        )
    raw_usage = _raw_usage(cap.exchanges)
    if not usage and raw_usage:
        usage = {k: raw_usage[k] for k in ("input_tokens", "output_tokens")}
    reported_cost = raw_usage.get("cost")
    reported = raw.get("model") if isinstance(raw.get("model"), str) else None
    return TaskResult(
        status=status,
        output=output,
        error=error,
        duration_ms=round((time.monotonic() - t0) * 1000),
        response_text=_response_text(raw),
        usage=usage,
        cost_usd=cost(rm, usage),
        cost_reported_usd=reported_cost,
        model_reported=reported,
        provider_reported=raw.get("provider") if isinstance(raw.get("provider"), str) else None,
        bridge_version=headers.get("x-bridge-version"),
        model_mismatch=reported is not None
        and not reported_model_matches(rm.config.model, reported),
        exchanges=cap.exchanges,
        tool_calls=tool_log(messages_seen),
        tools_sha256=tools_sha256,
    )
