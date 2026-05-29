"""Process-wide configuration for the platform API.

Scope is currently just ClickHouse — other platform config (SQLite path,
host/port, keystore) stays in its existing places and is folded in here
when it starts to feel scattered. Env prefix is FORTIFY_ to match the
existing convention (FORTIFY_KEYSTORE_PATH, FORTIFY_KEY, etc.).

Defaults point at the local docker-compose service so `make platform-api`
works out of the box once `make clickhouse-up` is running.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FORTIFY_")

    # ClickHouse audit log connection. Port 8124 matches the host-port
    # offset in platform/docker-compose.yml (the container still listens
    # on 8123 internally).
    clickhouse_host:     str  = "localhost"
    clickhouse_port:     int  = 8124
    clickhouse_user:     str  = "fortify"
    clickhouse_password: str  = "fortify-dev-password"
    clickhouse_database: str  = "fortify_audit"
    clickhouse_secure:   bool = False


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
