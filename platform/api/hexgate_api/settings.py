"""Process-wide configuration. HEXGATE_ env prefix matches existing convention."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_CLICKHOUSE_PASSWORD = "hexgate-dev-password"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_DASHBOARD_URL = "http://localhost:5173"


class Settings(BaseSettings):
    # Empty means unset: .env.sample ships keys with no value, and an empty
    # bool or int would otherwise refuse to load.
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="HEXGATE_", env_ignore_empty=True
    )

    clickhouse_host: str = "localhost"
    # 8124 matches platform/docker-compose.yml's host-port offset.
    clickhouse_port: int = 8124
    clickhouse_user: str = "hexgate"
    clickhouse_password: str = _DEV_CLICKHOUSE_PASSWORD
    clickhouse_database: str = "hexgate_audit"
    clickhouse_secure: bool = False

    # Span-enricher job (jobs/enricher). Redpanda is Kafka-protocol-compatible,
    # so these feed a stock Kafka client (aiokafka). localhost:9092 is the host
    # listener (platform/docker-compose.yml); in-compose consumers use
    # redpanda:29092. Same env var the collector and create-topics.sh read.
    redpanda_bootstrap_server: str = "localhost:9092"
    redpanda_raw_topic: str = "hexgate.otlp.raw"
    redpanda_dlq_topic: str = "hexgate.otlp.dlq"
    enricher_consumer_group: str = "hexgate-enricher"
    enricher_poll_timeout_ms: int = 1000
    enricher_max_poll_records: int = 500
    enricher_insert_max_backoff_s: float = 30.0

    # OAuth for MCP clients. public_url is the token `iss`, the base of every
    # URL in the authorization-server metadata, and the `aud` the API accepts.
    # A token's `aud` lists mcp_resource_url too, since the MCP server forwards
    # it to the API; in dev the MCP server listens on :8080 with no proxy in
    # front, so it is not public_url + "/mcp".
    public_url: str = "http://localhost:8000"
    mcp_resource_url: str = "http://localhost:8080/mcp"
    # The dashboard's origin, where /authorize sends a login for consent. In
    # dev it is the Vite server, not public_url.
    dashboard_url: str = _DEFAULT_DASHBOARD_URL
    # Also accept http client ids on a loopback host. Dev and tests only.
    oauth_allow_insecure_client_ids: bool = False

    @model_validator(mode="after")
    def _refuse_public_url_trailing_slash(self) -> "Settings":
        # The metadata's issuer must equal the token's `iss` character for
        # character, and the endpoint URLs are appended to it.
        if self.public_url.endswith("/"):
            raise ValueError(
                f"public_url={self.public_url!r} ends with '/' — set "
                "HEXGATE_PUBLIC_URL without the trailing slash."
            )
        return self

    @field_validator("dashboard_url", mode="before")
    @classmethod
    def _dashboard_url_absolute(cls, value: str) -> str:
        # Every login is redirected here, and email links point at it.
        # Stripped first: a CRLF env file leaves a "\r" that would end up in
        # the redirect's Location header. urlsplit drops tabs and newlines
        # before parsing, and gives an empty query for a bare "?", so control
        # characters, "?" and "#" are refused on the raw text.
        value = value.strip()
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None  # any userinfo, password included
            or "?" in value
            or "#" in value
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        ):
            raise ValueError(
                f"dashboard_url={value!r} must be an absolute http(s) URL with "
                "no user, query or fragment — set HEXGATE_DASHBOARD_URL."
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def _refuse_insecure_client_ids_on_remote_host(self) -> "Settings":
        # Accepting http client ids is for a client on the developer's own
        # machine; on a deployed API it would let any caller have it fetch from
        # the server's own loopback.
        # Both URLs: a deploy that leaves public_url at its default still
        # sets the dashboard's.
        for name, url in (
            ("public_url", self.public_url),
            ("dashboard_url", self.dashboard_url),
        ):
            host = urlsplit(url).hostname
            if self.oauth_allow_insecure_client_ids and host not in _LOCAL_HOSTS:
                raise ValueError(
                    f"oauth_allow_insecure_client_ids is set but {name} host "
                    f"{host!r} is not local — unset HEXGATE_OAUTH_ALLOW_INSECURE_CLIENT_IDS."
                )
        return self

    @model_validator(mode="after")
    def _refuse_dev_password_on_remote_host(self) -> "Settings":
        # The dev default keeps `make clickhouse-up` zero-setup, but pointing
        # it at a remote instance is always a misconfiguration — fail fast.
        if (
            self.clickhouse_password == _DEV_CLICKHOUSE_PASSWORD
            and self.clickhouse_host not in _LOCAL_HOSTS
        ):
            raise ValueError(
                f"clickhouse_host={self.clickhouse_host!r} is non-local but "
                "clickhouse_password is the committed dev default — set "
                "HEXGATE_CLICKHOUSE_PASSWORD to the real credential."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
