"""Settings from the environment, secrets from a secrets directory or the environment.

Plain settings use the prefix ``BENCH_``. Secrets (API keys, OIDC client secret, session
secret) are looked up by name: first ``<secrets_dir>/<NAME>`` (Podman/Docker/Kubernetes
secrets), then the environment variable ``NAME`` (fly.io secrets, local ``.env``).
Secret values are never logged or stored.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

SESSION_SECRET = "BENCH_SESSION_SECRET"
OIDC_CLIENT_SECRET = "BENCH_OIDC_CLIENT_SECRET"
RIS_MCP_API_KEY = "RIS_MCP_API_KEY"  # shared by the ris-mcp server and the worker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BENCH_", env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    config_path: Path = Field(default=Path("/config/providers.yaml"), alias="BENCH_CONFIG")
    secrets_dir: Path = Path("/run/secrets")
    database_url: str = ""
    public_url: str = "http://localhost:8000"

    session_hours: int = 8
    cost_cap_total_usd: float | None = None  # over all orders; None = no total cap
    worker_concurrency: int = 8  # LLM calls in flight per worker, over all providers
    dev_login: bool = False
    ris_mcp_url: str = "http://ris-mcp:8000/mcp"  # condition C
    ris_mcp_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    oidc_issuer: str = ""
    oidc_client_id: str = ""
    admin_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("admin_emails", "ris_mcp_allowed_hosts", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> object:
        if isinstance(value, str):
            return [e.strip() for e in value.split(",") if e.strip()]
        return value

    @field_validator("admin_emails")
    @classmethod
    def _lower_emails(cls, value: list[str]) -> list[str]:
        return [e.lower() for e in value]

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'bench.sqlite'}"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    def secret(self, name: str) -> str | None:
        """Value of the secret ``name``, or None if it is not set anywhere."""
        path = self.secrets_dir / name
        try:
            value = path.read_text().strip()
        except OSError:
            value = os.environ.get(name, "").strip()
        return value or None

    def has_secret(self, name: str) -> bool:
        return self.secret(name) is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
