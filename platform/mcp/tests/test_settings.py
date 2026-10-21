import pytest

from hexgate_mcp.settings import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HEXGATE_MCP_ISSUER", raising=False)
    monkeypatch.delenv("HEXGATE_PUBLIC_URL", raising=False)


def test_from_env_happy_path():
    assert Settings.from_env().issuer == "http://localhost:8000"


def test_when_public_url_set_then_issuer_follows_it(monkeypatch):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", "https://app.hexgate.test")

    assert Settings.from_env().issuer == "https://app.hexgate.test"


def test_when_mcp_issuer_set_then_it_wins_over_public_url(monkeypatch):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", "https://app.hexgate.test")
    monkeypatch.setenv("HEXGATE_MCP_ISSUER", "https://issuer.hexgate.test")

    assert Settings.from_env().issuer == "https://issuer.hexgate.test"


@pytest.mark.parametrize("var", ["HEXGATE_MCP_ISSUER", "HEXGATE_PUBLIC_URL"])
def test_when_issuer_ends_with_slash_then_start_up_refuses(monkeypatch, var):
    monkeypatch.setenv(var, "https://app.hexgate.test/")

    with pytest.raises(ValueError, match="trailing slash"):
        Settings.from_env()
