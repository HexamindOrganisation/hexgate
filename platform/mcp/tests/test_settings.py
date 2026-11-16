import logging
import runpy
from pathlib import Path
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
    existed = (dirs / directory).exists()

    with pytest.raises(ValueError, match=error):
        Settings.from_env()

    assert (dirs / directory).exists() == existed


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


def test_when_dir_is_missing_then_start_up_creates_it(monkeypatch, dirs, caplog):
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(dirs / "new" / "sub"))

    with caplog.at_level(logging.INFO, logger="hexgate_mcp.settings"):
        assert Settings.from_env().local_policy_dir == dirs / "new" / "sub"

    assert (dirs / "new" / "sub").is_dir()
    assert "created the local policy directory" in caplog.text


# A missing root can be no repo root, but can still be the home directory or
# a system one; neither exists in tmp_path, so each is pointed at it.
MISSING_REFUSALS = {
    "home": lambda monkeypatch, new: monkeypatch.setenv("HOME", str(new)),
    "system dir": lambda monkeypatch, new: monkeypatch.setattr(
        "hexgate_mcp.settings.SYSTEM_DIRS", [str(new)]
    ),
}


@pytest.mark.parametrize("point_at", MISSING_REFUSALS.values(), ids=MISSING_REFUSALS)
def test_when_missing_dir_is_unsafe_then_start_up_refuses_and_creates_nothing(
    monkeypatch, dirs, point_at
):
    point_at(monkeypatch, dirs / "new")
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(dirs / "new"))

    with pytest.raises(ValueError, match="policies folder"):
        Settings.from_env()

    assert not (dirs / "new").exists()


def test_when_dir_starts_with_a_tilde_then_it_lands_in_home(monkeypatch, dirs):
    monkeypatch.chdir(dirs)
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", "~/policies")

    assert Settings.from_env().local_policy_dir == dirs / "home" / "policies"
    assert (dirs / "home" / "policies").is_dir() and not (dirs / "~").exists()


# Each one used to be created below the cwd, where nobody looks.
@pytest.mark.parametrize(
    "raw",
    ["$HOME/policies", '"~/policies"', "policies"],
    ids=["unexpanded", "quoted", "relative"],
)
def test_when_dir_is_not_absolute_then_start_up_refuses_and_creates_nothing(
    monkeypatch, dirs, raw
):
    monkeypatch.chdir(dirs)
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", raw)
    before = sorted(dirs.rglob("*"))

    with pytest.raises(
        ValueError, match="HEXGATE_MCP_LOCAL_POLICY_DIR.*not an absolute"
    ):
        Settings.from_env()

    assert sorted(dirs.rglob("*")) == before


def test_when_dir_is_a_dangling_symlink_then_start_up_creates_its_target(
    monkeypatch, dirs
):
    (dirs / "link").symlink_to(dirs / "gone")
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(dirs / "link"))

    assert Settings.from_env().local_policy_dir == dirs / "link"
    assert (dirs / "gone").is_dir()


@pytest.mark.parametrize(
    "raw",
    ["~nosuchuser-hexgate/policies", "{dirs}/file/sub"],
    ids=["unknown user", "under a file"],
)
def test_when_dir_cannot_be_made_then_start_up_names_the_variable(
    monkeypatch, dirs, raw
):
    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", raw.format(dirs=dirs))

    with pytest.raises(ValueError, match="HEXGATE_MCP_LOCAL_POLICY_DIR"):
        Settings.from_env()


def test_when_dir_appears_during_start_up_then_start_up_keeps_it(monkeypatch, dirs):
    # A second server creates it after this one saw it missing.
    mkdir = Path.mkdir

    def race_then_mkdir(self, *args, **kwargs):
        if self == dirs / "new":
            mkdir(self)
        return mkdir(self, *args, **kwargs)

    monkeypatch.setenv("HEXGATE_MCP_LOCAL_POLICY_DIR", str(dirs / "new"))
    monkeypatch.setattr(Path, "mkdir", race_then_mkdir)

    assert Settings.from_env().local_policy_dir == dirs / "new"
