"""Process-wide configuration. FORTIFY_ env prefix matches existing convention."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FORTIFY_")

    clickhouse_host:     str  = "localhost"
    # 8124 matches platform/docker-compose.yml's host-port offset.
    clickhouse_port:     int  = 8124
    clickhouse_user:     str  = "fortify"
    clickhouse_password: str  = "fortify-dev-password"
    clickhouse_database: str  = "fortify_audit"
    clickhouse_secure:   bool = False


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
