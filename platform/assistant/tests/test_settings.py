from hexgate_assistant.settings import Settings


def test_from_env_happy_path(monkeypatch):
    monkeypatch.delenv("HEXGATE_ASSISTANT_BIND_HOST", raising=False)

    settings = Settings.from_env()

    assert settings.bind_host == "127.0.0.1"
    assert settings.port == 8090


def test_when_bind_host_is_set_then_from_env_uses_it(monkeypatch):
    monkeypatch.setenv("HEXGATE_ASSISTANT_BIND_HOST", "0.0.0.0")

    assert Settings.from_env().bind_host == "0.0.0.0"
