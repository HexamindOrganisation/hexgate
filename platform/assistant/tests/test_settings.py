import pytest

from hexgate_assistant.settings import Settings


@pytest.mark.parametrize("bind_host", [None, ""])
def test_from_env_happy_path(monkeypatch, bind_host):
    if bind_host is None:
        monkeypatch.delenv("HEXGATE_ASSISTANT_BIND_HOST", raising=False)
    else:
        monkeypatch.setenv("HEXGATE_ASSISTANT_BIND_HOST", bind_host)

    settings = Settings.from_env()

    assert settings.bind_host == "127.0.0.1"
    assert settings.port == 8090


def test_when_bind_host_is_set_then_from_env_uses_it(monkeypatch):
    monkeypatch.setenv("HEXGATE_ASSISTANT_BIND_HOST", "0.0.0.0")

    assert Settings.from_env().bind_host == "0.0.0.0"


def test_when_model_settings_are_set_then_from_env_reads_them(monkeypatch):
    monkeypatch.setenv("HEXGATE_ASSISTANT_MODEL", "openrouter:anthropic/claude-x")
    monkeypatch.setenv("HEXGATE_ASSISTANT_OPENROUTER_KEY", "sk-or-secret")
    monkeypatch.setenv("HEXGATE_ASSISTANT_MCP_URL", "http://mcp:8080/mcp")

    settings = Settings.from_env()

    assert settings.model == "openrouter:anthropic/claude-x"
    assert settings.openrouter_key == "sk-or-secret"
    assert settings.mcp_url == "http://mcp:8080/mcp"


@pytest.mark.parametrize("value", [None, ""])
def test_when_model_settings_are_unset_or_empty_then_there_is_no_model(
    monkeypatch, value
):
    for name in (
        "HEXGATE_ASSISTANT_MODEL",
        "HEXGATE_ASSISTANT_OPENROUTER_KEY",
        "HEXGATE_ASSISTANT_MCP_URL",
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.model is None
    assert settings.openrouter_key is None
    assert settings.mcp_url == "http://localhost:8080/mcp"


def test_when_settings_are_printed_then_the_openrouter_key_is_not():
    settings = Settings(model="openrouter:a/b", openrouter_key="sk-or-secret")

    assert "sk-or-secret" not in repr(settings)
