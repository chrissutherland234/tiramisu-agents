"""Environment-backed application settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TIRAMISU_",
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
    allow_unsafe_development_tenant_header: bool = False
    load_fictional_example_processes: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
