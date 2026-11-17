from hexgate_assistant.settings import Settings


def test_from_env_happy_path():
    settings = Settings.from_env()

    assert settings.bind_host == "0.0.0.0"
    assert settings.port == 8090
