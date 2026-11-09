"""Standalone Xource settings: PostgreSQL + local content-addressed disk by default."""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
# forwarded-auth reads process environment; load the same local .env once,
# without overriding explicit environment supplied by deployment or CI.
load_dotenv(ENV_FILE, override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")
    app_env: str = "prod"
    environment: str = "production"
    app_base_url: str = ""
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://xource:xource@localhost:5432/xource"
    storage_path: Path = Path("./data/blobs")
    max_upload_mb: int = Field(default=2048, gt=0)
    space_quota_mb: int = Field(default=0, ge=0)
    clamav_enabled: bool = False
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    clamav_timeout_s: float = 30
    clamav_max_scan_mb: int = 100
    document_max_mb: int = 25
    document_timeout_s: float = 20
    document_index_interval_s: int = 60
    document_index_enabled: bool = True
    document_processing_enabled: bool = False
    document_parser_mode: Literal["required", "local-unsafe"] = "required"
    cors_origins: list[str] = ["http://localhost:5173"]
    # Two explicit settings in forwarded-auth are needed to bypass auth in local development.
    # Production must never run without a verifying issuer + audience.
    @property
    def is_prod(self) -> bool:
        return self.environment == "production" or self.app_env == "prod"

    @model_validator(mode="after")
    def normalize_database_driver(self):
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        elif self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql+psycopg://", 1)
        return self


@lru_cache

def get_settings() -> Settings:
    return Settings()


settings = get_settings()
