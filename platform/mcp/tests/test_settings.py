import pytest

from hexgate_mcp.settings import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HEXGATE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("HEXGATE_MCP_LOCAL_POLICY_DIR", raising=False)


def test_from_env_happy_path():
    assert Settings.from_env().issuer == "http://localhost:8000"


def test_when_public_url_set_then_issuer_follows_it(monkeypatch):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", "https://app.hexgate.test")

    assert Settings.from_env().issuer == "https://app.hexgate.test"


def test_when_public_url_ends_with_slash_then_start_up_refuses(monkeypatch):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", "https://app.hexgate.test/")

    with pytest.raises(ValueError, match="trailing slash"):
        Settings.from_env()


def test_when_local_policy_dir_is_unset_then_it_is_none():
    assert Settings.from_env().local_policy_dir is None


def test_when_local_policy_dir_is_set_then_settings_carry_it(monkeypatch, tmp_path):
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(tmp_path))

    assert Settings.from_env().local_policy_dir == tmp_path


def test_when_local_policy_dir_is_missing_then_start_up_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(tmp_path / "missing"))

    with pytest.raises(ValueError, match="not a directory"):
        Settings.from_env()


def test_when_local_policy_dir_is_a_file_then_start_up_refuses(monkeypatch, tmp_path):
    (tmp_path / "policies").write_text("")
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(tmp_path / "policies"))

    with pytest.raises(ValueError, match="not a directory"):
        Settings.from_env()


@pytest.mark.parametrize("issuer", ["http://localhost:8000", "http://127.0.0.1:8000"])
def test_when_issuer_is_local_then_local_policy_dir_is_allowed(
    monkeypatch, tmp_path, issuer
):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", issuer)
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(tmp_path))

    assert Settings.from_env().local_policy_dir == tmp_path


@pytest.mark.parametrize(
    "issuer", ["https://app.hexgate.test", "http://localhost.hexgate.test"]
)
def test_when_issuer_is_not_local_then_local_policy_dir_refuses_start_up(
    monkeypatch, tmp_path, issuer
):
    monkeypatch.setenv("HEXGATE_PUBLIC_URL", issuer)
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="local development only"):
        Settings.from_env()
