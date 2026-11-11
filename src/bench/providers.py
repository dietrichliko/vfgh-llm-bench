"""Providers and models from ``providers.yaml``, turned into Pydantic AI models.

Every provider is an OpenAI-compatible endpoint. The file names secrets, it never contains
them. Each resolved model carries a hash of its configuration, which runs store.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
import yaml
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import NativeOutput, PromptedOutput
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from bench.llm.schemas import PROMPTED_OUTPUT_TEMPLATE
from bench.settings import Settings


class OutputMode(StrEnum):
    NATIVE = "native"  # JSON schema via response_format
    PROMPTED = "prompted"  # JSON requested in the prompt


class ToolMode(StrEnum):
    NATIVE = "native"  # the only mode allowed in condition C
    EMULATED = "emulated"  # e.g. academic-ai-bridge for u:ai
    NONE = "none"


class Role(StrEnum):
    TESTED = "tested"
    JUDGE = "judge"
    EXTRACTOR = "extractor"
    WRITER = "writer"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Price(_Strict):
    """USD per million tokens, only for estimates."""

    input: float = Field(alias="in", ge=0)
    output: float = Field(alias="out", ge=0)


class ProviderConfig(_Strict):
    base_url: str
    api_key_secret: str
    headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    output_mode: OutputMode = OutputMode.NATIVE
    # prompted mode only: also send response_format {"type": "json_object"}
    json_object: bool = False
    tool_mode: ToolMode = ToolMode.NONE
    max_concurrency: int = Field(default=4, ge=1)
    requests_per_minute: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=600, gt=0)  # without any data from the endpoint
    # wall clock for a whole run incl. tool rounds; keep-alive data does not reset it
    max_run_seconds: float = Field(default=900, gt=0)
    # cap on the answer incl. thinking, sent as max_tokens; a model may override it.
    # None: no cap (a model caught in a loop then runs until its own limit)
    max_output_tokens: int | None = Field(default=None, ge=1)
    # the request field for that cap: OpenRouter and most endpoints read max_tokens,
    # OpenAI and the u:ai bridge max_completion_tokens; a field the endpoint ignores
    # would leave the answer uncapped without any error
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"


class ModelConfig(_Strict):
    provider: str
    model: str
    display_name: str | None = None
    family: str
    cutoff: date | None = None
    roles: list[Role] = Field(min_length=1)
    price: Price | None = None
    expected_output_tokens: int = Field(default=6000, ge=1)  # incl. thinking; for estimates
    max_output_tokens: int | None = Field(default=None, ge=1)  # overrides the provider's
    parameters: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _tested_needs_cutoff(self) -> "ModelConfig":
        if Role.TESTED in self.roles and self.cutoff is None:
            raise ValueError("a tested model needs a training cutoff ('cutoff')")
        return self


class StudyConfig(_Strict):
    providers: dict[str, ProviderConfig] = Field(min_length=1)
    models: dict[str, ModelConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _known_providers(self) -> "StudyConfig":
        for key, m in self.models.items():
            if m.provider not in self.providers:
                raise ValueError(f"model '{key}': unknown provider '{m.provider}'")
        return self


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of silently keeping the last one."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key '{key}'", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def parse_config(text: str) -> StudyConfig:
    data = yaml.load(text, Loader=_UniqueKeyLoader)  # SafeLoader subclass
    return StudyConfig.model_validate(data)


def load_config(path: Path) -> StudyConfig:
    return parse_config(path.read_text())


@dataclass(frozen=True)
class ResolvedModel:
    key: str
    config: ModelConfig
    provider_key: str
    provider: ProviderConfig
    available: bool  # False if the provider's secret is missing
    config_hash: str

    @property
    def name(self) -> str:
        return self.config.display_name or self.key


def config_hash(provider: ProviderConfig, model: ModelConfig) -> str:
    data = {
        "provider": provider.model_dump(mode="json", by_alias=True),
        "model": model.model_dump(mode="json", by_alias=True),
    }
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def resolve(config: StudyConfig, settings: Settings) -> dict[str, ResolvedModel]:
    out: dict[str, ResolvedModel] = {}
    for key, m in config.models.items():
        p = config.providers[m.provider]
        out[key] = ResolvedModel(
            key=key,
            config=m,
            provider_key=m.provider,
            provider=p,
            available=settings.has_secret(p.api_key_secret),
            config_hash=config_hash(p, m),
        )
    return out


def max_output_tokens(rm: ResolvedModel) -> int | None:
    return rm.config.max_output_tokens or rm.provider.max_output_tokens


def routing_problem(rm: ResolvedModel) -> str | None:
    """Why the endpoint may answer with a different upstream from call to call, or None.

    OpenRouter picks a provider per call (with other quantisations and defaults) unless
    the request names one: ``provider: {order: [...], allow_fallbacks: false}``. Other
    endpoints serve one upstream and count as pinned.
    """
    if "openrouter.ai" not in rm.provider.base_url:
        return None
    routing = {**rm.provider.extra_body, **rm.config.extra_body}.get("provider") or {}
    if not (routing.get("order") or routing.get("only")):
        return "OpenRouter-Anbieter nicht festgelegt (provider.order)"
    if routing.get("allow_fallbacks") is not False:
        return "OpenRouter darf auf andere Anbieter ausweichen (allow_fallbacks: false fehlt)"
    return None


SAMPLING = ("temperature",)  # what the protocol expects to be stated per tested model


def model_settings(rm: ResolvedModel) -> OpenAIChatModelSettings:
    """Settings sent with every request: only what the config states, nothing invented."""
    settings: dict[str, Any] = {}
    if cap := max_output_tokens(rm):
        settings["max_tokens"] = cap
    settings.update(rm.config.parameters)  # an explicit parameter wins
    extra_body = {**rm.provider.extra_body, **rm.config.extra_body}
    if extra_body:
        settings["extra_body"] = extra_body
    if rm.provider.headers:
        settings["extra_headers"] = dict(rm.provider.headers)
    return cast(OpenAIChatModelSettings, settings)


def build_model(
    rm: ResolvedModel, settings: Settings, http_client: httpx2.AsyncClient
) -> OpenAIChatModel:
    api_key = settings.secret(rm.provider.api_key_secret)
    if api_key is None:
        raise RuntimeError(f"secret {rm.provider.api_key_secret} is not set")
    client = AsyncOpenAI(
        base_url=rm.provider.base_url,
        api_key=api_key,
        max_retries=0,  # retries only in our transport (bench.llm.http)
        timeout=rm.provider.timeout_seconds,
        http_client=http_client,
    )
    provider = OpenAIProvider(openai_client=client)
    # what the endpoint supports comes from our config, not from guesses by model name
    base: dict[str, Any] = dict(provider.model_profile(rm.config.model) or {})
    profile = cast(
        OpenAIModelProfile,
        {
            **base,
            "supports_json_schema_output": rm.provider.output_mode is OutputMode.NATIVE,
            "supports_json_object_output": rm.provider.json_object,
            "openai_chat_supports_max_completion_tokens": (
                rm.provider.max_tokens_field == "max_completion_tokens"
            ),
        },
    )
    return OpenAIChatModel(
        rm.config.model,
        provider=provider,
        profile=profile,
        settings=model_settings(rm),
    )


def output_spec[T](rm: ResolvedModel, output_type: type[T]) -> NativeOutput[T] | PromptedOutput[T]:
    """JSON schema via the API where supported, otherwise our own (versioned) prompt text."""
    if rm.provider.output_mode is OutputMode.NATIVE:
        return NativeOutput(output_type)
    return PromptedOutput(output_type, template=PROMPTED_OUTPUT_TEMPLATE)
