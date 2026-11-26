import pytest

from hexgate_assistant.settings import Settings

# Unset and empty (compose passes `KEY=` through as "") read the same.
EMPTY_MEANS_UNSET = [
    "HEXGATE_ASSISTANT_BIND_HOST",
    "HEXGATE_ASSISTANT_MODEL",
    "HEXGATE_ASSISTANT_OPENROUTER_KEY",
    "HEXGATE_ASSISTANT_MCP_URL",
]
ENV = [
    *EMPTY_MEANS_UNSET,
    "HEXGATE_PUBLIC_URL",
    "HEXGATE_ASSISTANT_RESOURCE_URL",
    "HEXGATE_ASSISTANT_API_URL",
    "HEXGATE_ASSISTANT_POSTGRES_USER",
    "HEXGATE_ASSISTANT_POSTGRES_HOST",
    "HEXGATE_ASSISTANT_POSTGRES_PORT",
    "HEXGATE_ASSISTANT_POSTGRES_DB",
    "HEXGATE_ASSISTANT_CONTEXT_TOKENS",
]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HEXGATE_ASSISTANT_POSTGRES_PASSWORD", "pw")


def test_from_env_happy_path():
    settings = Settings.from_env()

    assert settings.bind_host == "127.0.0.1"
    assert settings.port == 8090
    assert settings.model is None
    assert settings.openrouter_key is None
    assert settings.mcp_url == "http://localhost:8080/mcp"
    assert settings.context_tokens == 64_000
    assert settings.issuer == "http://localhost:8000"
    assert settings.resource_url == "http://localhost:8090/v1/assistant"
    assert settings.jwks_url == "http://localhost:8000/.well-known/jwks.json"
    assert settings.database_url.render_as_string(hide_password=False) == (
        "postgresql+asyncpg://hexgate_assistant:pw@postgres:5432/hexgate_assistant"
    )
    assert "pw" not in repr(settings)


def test_when_variables_are_set_then_from_env_uses_them(monkeypatch):
    monkeypatch.setenv("HEXGATE_ASSISTANT_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", "https://app.hexgate.ai")
    monkeypatch.setenv(
        "HEXGATE_ASSISTANT_RESOURCE_URL", "https://app.hexgate.ai/v1/assistant"
    )
    monkeypatch.setenv("HEXGATE_ASSISTANT_API_URL", "http://api:8000/")
    monkeypatch.setenv("HEXGATE_ASSISTANT_POSTGRES_USER", "u")
    monkeypatch.setenv("HEXGATE_ASSISTANT_POSTGRES_HOST", "db")
    monkeypatch.setenv("HEXGATE_ASSISTANT_POSTGRES_PORT", "6543")
    monkeypatch.setenv("HEXGATE_ASSISTANT_POSTGRES_DB", "d")
    monkeypatch.setenv("HEXGATE_ASSISTANT_MODEL", "openrouter:anthropic/claude-x")
    monkeypatch.setenv("HEXGATE_ASSISTANT_OPENROUTER_KEY", "sk-or-secret")
    monkeypatch.setenv("HEXGATE_ASSISTANT_MCP_URL", "http://mcp:8080/mcp")
    monkeypatch.setenv("HEXGATE_ASSISTANT_CONTEXT_TOKENS", "32000")

    settings = Settings.from_env()

    assert settings.bind_host == "0.0.0.0"
    assert settings.model == "openrouter:anthropic/claude-x"
    assert settings.openrouter_key == "sk-or-secret"
    assert settings.mcp_url == "http://mcp:8080/mcp"
    assert settings.context_tokens == 32_000
    assert settings.issuer == "https://app.hexgate.ai"
    assert settings.resource_url == "https://app.hexgate.ai/v1/assistant"
    assert settings.jwks_url == "http://api:8000/.well-known/jwks.json"
    url = settings.database_url.render_as_string(hide_password=False)
    assert url == "postgresql+asyncpg://u:pw@db:6543/d"


def test_when_password_has_reserved_characters_then_url_escapes_them(monkeypatch):
    monkeypatch.setenv("HEXGATE_ASSISTANT_POSTGRES_PASSWORD", "p@ss:w/rd#?")

    url = Settings.from_env().database_url.render_as_string(hide_password=False)

    assert ":p%40ss%3Aw%2Frd%23%3F@postgres:" in url


def test_when_password_is_unset_then_from_env_refuses(monkeypatch):
    monkeypatch.delenv("HEXGATE_ASSISTANT_POSTGRES_PASSWORD")

    with pytest.raises(ValueError, match="HEXGATE_ASSISTANT_POSTGRES_PASSWORD"):
        Settings.from_env()


def test_when_public_url_ends_with_slash_then_from_env_refuses(monkeypatch):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", "https://app.hexgate.ai/")

    with pytest.raises(ValueError, match="trailing slash"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["abc", "5432x", "0", "65536", "-1"])
def test_when_port_is_not_a_port_number_then_from_env_names_the_variable(
    monkeypatch, value
):
    monkeypatch.setenv("HEXGATE_ASSISTANT_POSTGRES_PORT", value)

    with pytest.raises(ValueError, match="HEXGATE_ASSISTANT_POSTGRES_PORT"):
        Settings.from_env()


def test_when_port_is_empty_then_from_env_uses_the_default(monkeypatch):
    monkeypatch.setenv("HEXGATE_ASSISTANT_POSTGRES_PORT", "")

    assert Settings.from_env().database_url.port == 5432


def test_when_variables_are_empty_then_from_env_reads_them_as_unset(monkeypatch):
    for name in EMPTY_MEANS_UNSET:
        monkeypatch.setenv(name, "")

    settings = Settings.from_env()

    assert settings.bind_host == "127.0.0.1"
    assert settings.model is None
    assert settings.openrouter_key is None
    assert settings.mcp_url == "http://localhost:8080/mcp"


def test_when_settings_are_printed_then_the_openrouter_key_is_not(monkeypatch):
    monkeypatch.setenv("HEXGATE_ASSISTANT_OPENROUTER_KEY", "sk-or-secret")

    assert "sk-or-secret" not in repr(Settings.from_env())


@pytest.mark.parametrize("raw", ["0", "-5", "lots"])
def test_when_context_tokens_is_not_a_positive_number_then_from_env_refuses(
    monkeypatch, raw
):
    monkeypatch.setenv("HEXGATE_ASSISTANT_CONTEXT_TOKENS", raw)

    with pytest.raises(ValueError, match="HEXGATE_ASSISTANT_CONTEXT_TOKENS"):
        Settings.from_env()
