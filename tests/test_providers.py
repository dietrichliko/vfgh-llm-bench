from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from pydantic_ai import NativeOutput, PromptedOutput

from bench.providers import (
    ToolMode,
    load_config,
    model_settings,
    output_spec,
    parse_config,
    resolve,
    routing_problem,
)
from bench.settings import Settings
from tests.conftest import CONFIG

ROOT = Path(__file__).parent.parent


def test_example_config_is_valid() -> None:
    config = load_config(ROOT / "config" / "providers.example.yaml")
    assert config.providers["openrouter"].tool_mode is ToolMode.NATIVE
    assert config.providers["uai"].tool_mode is ToolMode.EMULATED


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown provider 'nope'"):
        parse_config(CONFIG.replace("provider: openrouter", "provider: nope"))


def test_duplicate_key_rejected() -> None:
    text = CONFIG + "  glm:\n    provider: uai\n    model: x\n    family: y\n    roles: [judge]\n"
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key 'glm'"):
        parse_config(text)


def test_tested_model_needs_cutoff() -> None:
    with pytest.raises(ValidationError, match="training cutoff"):
        parse_config(CONFIG.replace("    cutoff: 2026-01-01\n", ""))


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_config(CONFIG.replace("family: zhipu", "family: zhipu\n    temprature: 0"))


def test_missing_secret_marks_model_unavailable(settings: Settings) -> None:
    models = resolve(parse_config(CONFIG), settings)
    assert models["judge"].available  # TEST_UAI_KEY is in the secrets dir
    assert not models["glm"].available  # TEST_OPENROUTER_KEY is not set


def test_secret_from_environment_makes_model_available(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "k")
    assert resolve(parse_config(CONFIG), settings)["glm"].available


def test_model_settings_send_only_configured_values(settings: Settings) -> None:
    models = resolve(parse_config(CONFIG), settings)
    assert model_settings(models["judge"]) == {"extra_headers": {"X-Bridge-Raw": "1"}}
    assert model_settings(models["glm"]) == {"extra_body": {"provider": {"allow_fallbacks": False}}}


def test_config_hash_changes_with_config(settings: Settings) -> None:
    a = resolve(parse_config(CONFIG), settings)["glm"].config_hash
    b = resolve(parse_config(CONFIG), settings)["glm"].config_hash
    c = resolve(parse_config(CONFIG.replace("2026-01-01", "2026-02-01")), settings)["glm"]
    assert a == b != c.config_hash


def test_output_mode(settings: Settings) -> None:
    models = resolve(parse_config(CONFIG), settings)
    assert isinstance(output_spec(models["judge"], dict), PromptedOutput)
    assert isinstance(output_spec(models["glm"], dict), NativeOutput)


ROUTING = """
providers:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key_secret: K
    extra_body: {usage: {include: true}}
  direct:
    base_url: http://bridge/v1
    api_key_secret: K
models:
  free:
    provider: openrouter
    model: a/free
    family: a
    cutoff: 2025-01-01
    roles: [tested]
  fallback:
    provider: openrouter
    model: a/fallback
    family: a
    cutoff: 2025-01-01
    roles: [tested]
    extra_body: {provider: {order: [deepinfra/fp8]}}
  pinned:
    provider: openrouter
    model: a/pinned
    family: a
    cutoff: 2025-01-01
    roles: [tested]
    parameters: {temperature: 0.7}
    extra_body: {provider: {order: [deepinfra/fp8], allow_fallbacks: false}}
  bridge:
    provider: direct
    model: b
    family: b
    cutoff: 2025-01-01
    roles: [tested]
"""


def test_routing_problem(settings: Settings) -> None:
    models = resolve(parse_config(ROUTING), settings)
    problems = {k: routing_problem(rm) for k, rm in models.items()}
    assert problems["free"] and "provider.order" in problems["free"]
    assert problems["fallback"] and "allow_fallbacks" in problems["fallback"]
    assert problems["pinned"] is None and problems["bridge"] is None
