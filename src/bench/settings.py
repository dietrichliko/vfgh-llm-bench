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

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

SESSION_SECRET = "BENCH_SESSION_SECRET"
OIDC_CLIENT_SECRET = "BENCH_OIDC_CLIENT_SECRET"
RIS_MCP_API_KEY = "RIS_MCP_API_KEY"  # shared by the ris-mcp server and the worker
BACKUP_ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"  # off-site backup bucket (Tigris on fly.io)
BACKUP_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
FLY_API_TOKEN = "FLY_API_TOKEN"  # fly.io: deploy token of the bridge app only


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BENCH_", env_file=".env", extra="ignore", populate_by_name=True
    )

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
    # study repository for Freeze protocol / Publish results (stage 6d); empty = ZIP only.
    # The SSH deploy key is the secret STUDY_REPO_DEPLOY_KEY.
    study_repo_url: str = ""
    study_repo_branch: str = "main"
    git_commit: str = ""  # of this app, set at image build; recorded in environment.yaml
    image_digest: str = ""

    # `bench all` (fly.io): stop after this many minutes without requests or open jobs;
    # 0 = never. The web process marks activity by touching activity_file.
    idle_stop_minutes: int = 0
    activity_file: Path | None = None
    # off-site backup to an S3 bucket (fly.io: Tigris sets BUCKET_NAME and the AWS_*
    # secrets); empty = no off-site backup. Made by `bench all` only after changes.
    backup_bucket: str = Field(
        default="", validation_alias=AliasChoices("BENCH_BACKUP_BUCKET", "BUCKET_NAME")
    )
    backup_prefix: str = "backups/"
    backup_interval_hours: float = 6.0  # while the machine runs
    backup_keep_days: int = 14  # all backups this recent; older: the newest of each week
    # fly.io: `bench all` starts the machines of this app (the u:ai bridge) when it starts
    # and stops them when it stops; the worker calls it over the private network, not
    # through fly.io's proxy, which cuts requests silent for 60 s. Empty = not managed.
    fly_bridge_app: str = ""
    fly_bridge_health_url: str = ""  # waited for after the start, e.g. …internal:8000/health
    fly_api_url: str = "http://_api.internal:4280"  # Machines API inside fly.io

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
