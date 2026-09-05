"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal, Self
from uuid import UUID

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tiramisu_agents.budgets.pricing import ModelPriceOverride

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


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
    deployment_id: str | None = None
    deployment_build_id: str | None = None
    deployment_tenant_ids: tuple[UUID, ...] = Field(
        default=(),
        validation_alias=AliasChoices(
            "TIRAMISU_DEPLOYMENT_TENANT_IDS",
            "TIRAMISU_WORKER_TENANT_IDS",
        ),
    )
    openai_model: str | None = Field(default=None)
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "TIRAMISU_OPENAI_API_KEY"),
    )
    model_price_overrides: dict[str, ModelPriceOverride] = Field(default_factory=dict)
    max_model_tokens_per_tenant: int = Field(default=100_000_000, ge=0)
    max_model_cost_micros_per_tenant: int = Field(default=2_000_000_000, ge=0)
    platform_model_calls_paused: bool = False
    platform_outbound_messages_paused: bool = False
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

    @field_validator("client_pack_factory", "deployment_id", "deployment_build_id", mode="before")
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

    @field_validator("deployment_tenant_ids")
    @classmethod
    def require_unique_deployment_tenants(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(value) != len(set(value)):
            raise ValueError("deployment tenant assignments must be unique")
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
