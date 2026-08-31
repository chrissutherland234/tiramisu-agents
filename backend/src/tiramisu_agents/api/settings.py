"""Environment-backed application settings."""

import re
from functools import lru_cache
from typing import Literal, Self
from uuid import UUID

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_TASK_QUEUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TIRAMISU_",
        populate_by_name=True,
        extra="ignore",
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu"
    migration_database_url: str = "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu"
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "tiramisu-agent"
    openai_model: str | None = Field(default=None)
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "TIRAMISU_OPENAI_API_KEY"),
    )
    worker_tenant_ids: tuple[UUID, ...] = ()
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    allow_unsafe_development_tenant_header: bool = False
    load_fictional_example_processes: bool = False
    client_pack_factory: str | None = None

    @field_validator("openai_model", mode="before")
    @classmethod
    def normalize_optional_model(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("client_pack_factory", mode="before")
    @classmethod
    def normalize_optional_client_pack_factory(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def normalize_optional_api_key(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _LOG_LEVELS:
            raise ValueError(f"log level must be one of {sorted(_LOG_LEVELS)}")
        return normalized

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database URLs must use the postgresql+asyncpg driver")
        return value

    @field_validator("temporal_target", "temporal_namespace", "api_host")
    @classmethod
    def require_nonblank_runtime_value(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("runtime configuration values cannot be blank")
        return stripped

    @field_validator("temporal_task_queue")
    @classmethod
    def validate_task_queue(cls, value: str) -> str:
        stripped = value.strip()
        if not _TASK_QUEUE_PATTERN.fullmatch(stripped):
            raise ValueError("Temporal task queue has invalid characters or length")
        return stripped

    @field_validator("worker_tenant_ids")
    @classmethod
    def require_unique_worker_tenants(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(value) != len(set(value)):
            raise ValueError("worker tenant assignments must be unique")
        return value

    @model_validator(mode="after")
    def prevent_development_features_in_deployments(self) -> Self:
        if self.environment != "development" and self.allow_unsafe_development_tenant_header:
            raise ValueError("unsafe development identity headers require development environment")
        if (
            self.environment not in {"development", "test"}
            and self.load_fictional_example_processes
        ):
            raise ValueError("the fictional client pack is restricted to development and tests")
        if self.client_pack_factory and self.load_fictional_example_processes:
            raise ValueError(
                "configure either TIRAMISU_CLIENT_PACK_FACTORY or the fictional example"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
