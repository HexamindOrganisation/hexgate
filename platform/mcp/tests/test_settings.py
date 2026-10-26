import runpy
from unittest.mock import Mock

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


def test_when_local_policy_dir_is_unset_then_the_tool_is_off():
    settings = Settings.from_env()

    assert settings.local_policy_dir is None
    assert settings.bind_host == "0.0.0.0"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "worktree").mkdir()
    (tmp_path / "worktree" / ".git").write_text("gitdir: ../repo/.git")
    (tmp_path / "home").mkdir()
    (tmp_path / "policies").mkdir()
    (tmp_path / "file").write_text("")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.mark.parametrize(
    "issuer", [None, "http://localhost:8000", "http://127.0.0.1:8000"]
)
def test_when_dir_is_set_then_bind_host_is_loopback(monkeypatch, dirs, issuer):
    if issuer:
        monkeypatch.setenv("HEXGATE_PUBLIC_URL", issuer)
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(dirs / "policies"))

    settings = Settings.from_env()

    assert settings.local_policy_dir == dirs / "policies"
    assert settings.bind_host == "127.0.0.1"


NOT_LOCAL = "local development only"
# (directory under tmp_path, or an absolute one; issuer; error)
START_UP_REFUSALS = {
    "missing": ("missing", None, "not a directory"),
    "a file": ("file", None, "not a directory"),
    "remote issuer": ("policies", "https://app.hexgate.test", NOT_LOCAL),
    "stray in deploy": ("missing", "https://app.hexgate.test", NOT_LOCAL),
    "lookalike host": ("policies", "http://localhost.hexgate.test", NOT_LOCAL),
    "repo root": ("repo", None, "policies folder"),
    "worktree root": ("worktree", None, "policies folder"),
    "filesystem root": ("/", None, "policies folder"),
    "etc": ("/etc", None, "policies folder"),
    "home": ("home", None, "policies folder"),
    "above home": (".", None, "policies folder"),
}


@pytest.mark.parametrize(
    "directory, issuer, error", START_UP_REFUSALS.values(), ids=START_UP_REFUSALS
)
def test_when_local_policy_dir_is_unsafe_then_start_up_refuses(
    monkeypatch, dirs, directory, issuer, error
):
    if issuer:
        monkeypatch.setenv("HEXGATE_PUBLIC_URL", issuer)
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(dirs / directory))

    with pytest.raises(ValueError, match=error):
        Settings.from_env()


def test_when_dir_is_set_then_main_binds_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(tmp_path))
    monkeypatch.setattr("uvicorn.run", run := Mock())

    runpy.run_module("hexgate_mcp", run_name="__main__")

    assert run.call_args.kwargs["host"] == "127.0.0.1"


def test_when_dir_holds_repos_then_start_up_allows_it(monkeypatch, dirs):
    # ~/workspace with repos below: confine() refuses writes into them.
    (dirs / "workspace" / "app" / ".git").mkdir(parents=True)
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(dirs / "workspace"))

    assert Settings.from_env().local_policy_dir == dirs / "workspace"
