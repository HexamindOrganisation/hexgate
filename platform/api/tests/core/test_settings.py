"""Settings validation — the dev-default-password-on-remote-host guard and the
OAuth URL settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hexgate_api.settings import _DEV_CLICKHOUSE_PASSWORD, Settings


def _settings(**kwargs) -> Settings:
    # _env_file=None so a developer's local .env can't flip test outcomes.
    # The flag pinned off too: load_dotenv may already have exported it.
    return Settings(
        _env_file=None, **{"oauth_allow_insecure_client_ids": False, **kwargs}
    )


def test_local_host_with_dev_password_is_fine() -> None:
    s = _settings(
        clickhouse_host="localhost",
        clickhouse_password=_DEV_CLICKHOUSE_PASSWORD,
    )
    assert s.clickhouse_password == _DEV_CLICKHOUSE_PASSWORD


def test_remote_host_with_dev_password_refused() -> None:
    with pytest.raises(ValidationError, match="dev default"):
        _settings(
            clickhouse_host="clickhouse.prod.internal",
            clickhouse_password=_DEV_CLICKHOUSE_PASSWORD,
        )


def test_remote_host_with_real_password_is_fine() -> None:
    s = _settings(
        clickhouse_host="clickhouse.prod.internal",
        clickhouse_password="s3cret-rotated",
    )
    assert s.clickhouse_host == "clickhouse.prod.internal"


def test_public_url_trailing_slash_refused() -> None:
    with pytest.raises(ValidationError, match="trailing slash"):
        _settings(public_url="https://app.hexgate.ai/")


def test_oauth_urls_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # _env_file=None blocks .env only; a shell export would still win.
    monkeypatch.delenv("HEXGATE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("HEXGATE_MCP_RESOURCE_URL", raising=False)
    s = _settings()
    assert s.public_url == "http://localhost:8000"
    assert s.mcp_resource_url == "http://localhost:8080/mcp"


@pytest.mark.parametrize(
    "dashboard_url",
    [
        "dash.example",
        "ftp://dash.example",
        "javascript:alert(1)//",
        "https://u:p@dash.example",
        "https://u@dash.example",
        "https://:p@dash.example",
        "https://dash.example/?x=1",
        "https://dash.example#f",
        "https://dash.example/?",
        "https://dash.example#",
        "https://dash.example/a\x00b",
        "https://dash.example/a\tb",
        "https://dash\nexample",
    ],
)
def test_when_dashboard_url_not_absolute_http_then_refused(dashboard_url: str) -> None:
    with pytest.raises(ValidationError, match="dashboard_url"):
        _settings(dashboard_url=dashboard_url)


@pytest.mark.parametrize(
    ("dashboard_url", "expected"),
    [
        ("https://app.hexgate.ai/", "https://app.hexgate.ai"),
        # A CRLF env file, or stray spaces.
        ("https://app.hexgate.ai\r", "https://app.hexgate.ai"),
        (" https://app.hexgate.ai/ ", "https://app.hexgate.ai"),
        ("https://app.hexgate.ai/dash", "https://app.hexgate.ai/dash"),
    ],
)
def test_when_dashboard_url_set_then_normalised(
    dashboard_url: str, expected: str
) -> None:
    assert _settings(dashboard_url=dashboard_url).dashboard_url == expected


@pytest.mark.parametrize(
    ("name", "field", "default"),
    [
        ("DASHBOARD_URL", "dashboard_url", "http://localhost:5173"),
        ("CLICKHOUSE_SECURE", "clickhouse_secure", False),
    ],
)
def test_when_env_value_empty_then_default(
    monkeypatch: pytest.MonkeyPatch, name: str, field: str, default: object
) -> None:
    # .env.sample ships keys with no value; an empty bool would not parse.
    monkeypatch.setenv(f"HEXGATE_{name}", "")
    assert getattr(_settings(), field) == default


@pytest.fixture
def no_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # _env_file=None blocks .env only; exported values would still reach Settings.
    monkeypatch.delenv("HEXGATE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("HEXGATE_DASHBOARD_URL", raising=False)


@pytest.mark.usefixtures("no_url_env")
def test_when_insecure_client_ids_on_remote_public_url_then_refused() -> None:
    with pytest.raises(ValidationError, match="oauth_allow_insecure_client_ids"):
        _settings(
            public_url="https://app.hexgate.ai", oauth_allow_insecure_client_ids=True
        )


@pytest.mark.usefixtures("no_url_env")
def test_when_insecure_client_ids_on_remote_dashboard_url_then_refused() -> None:
    # A deploy that leaves public_url at its local default still sets this.
    with pytest.raises(ValidationError, match="dashboard_url host"):
        _settings(
            dashboard_url="https://app.hexgate.ai", oauth_allow_insecure_client_ids=True
        )


@pytest.mark.usefixtures("no_url_env")
def test_when_insecure_client_ids_on_local_public_url_then_allowed() -> None:
    s = _settings(
        public_url="http://127.0.0.1:8000", oauth_allow_insecure_client_ids=True
    )
    assert s.oauth_allow_insecure_client_ids
