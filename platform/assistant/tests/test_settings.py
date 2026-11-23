import pytest

from hexgate_assistant.settings import Settings

ENV = [
    "HEXGATE_ASSISTANT_BIND_HOST",
    "HEXGATE_PUBLIC_URL",
    "HEXGATE_ASSISTANT_RESOURCE_URL",
    "HEXGATE_ASSISTANT_API_URL",
    "HEXGATE_ASSISTANT_POSTGRES_USER",
    "HEXGATE_ASSISTANT_POSTGRES_HOST",
    "HEXGATE_ASSISTANT_POSTGRES_PORT",
    "HEXGATE_ASSISTANT_POSTGRES_DB",
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

    settings = Settings.from_env()

    assert settings.bind_host == "0.0.0.0"
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
