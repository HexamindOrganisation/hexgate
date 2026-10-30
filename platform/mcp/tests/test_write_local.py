import dataclasses
import json
import logging
import os
import shutil
import stat
import threading
from unittest.mock import Mock

import pytest
import respx
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.auth import HexgateAccessToken
from hexgate_mcp.tools import write_local
from tests.conftest import JWKS_URL, PROJECT, SETTINGS, jwks, mcp_client, post_mcp

LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
POLICY = "version: 1\nconstraints: []\n"
CAP = write_local.CONTENT_CAP_BYTES


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "policies"
    root.mkdir()
    return root


async def write(
    root, path: str, content: str = POLICY, scopes: str = "policy:write"
) -> str:
    token = HexgateAccessToken(
        token="t", client_id="c", scopes=scopes.split(), subject="s", projects=[PROJECT]
    )
    auth_context_var.set(AuthenticatedUser(token))
    mcp = FastMCP("test")
    write_local.register(mcp, root)
    result = await mcp.call_tool(
        "policy_write_local", {"path": path, "content": content}
    )
    return result[0].text


def existing(root, tmp_path):
    (root / "agent.yaml").write_text("old")
    return root


def linked_root(root, tmp_path):
    (tmp_path / "link").symlink_to(root)
    return tmp_path / "link"


def alias_inside(root, tmp_path):
    (root / "real.yaml").write_text("old")
    (root / "alias.yaml").symlink_to(root / "real.yaml")
    return root


def root_in_dot_directory(root, tmp_path):
    # Only the path below the root is checked: ~/.config/hexgate is a fine root.
    (tmp_path / ".config" / "policies").mkdir(parents=True)
    return tmp_path / ".config" / "policies"


# (setup, path, content, the file that ends up holding the content)
WRITES = {
    "new file": (None, "agent.yaml", POLICY, "agent.yaml"),
    "replaces": (existing, "agent.yaml", POLICY, "agent.yaml"),
    "subdirectories": (None, "org/team/agent.yaml", POLICY, "org/team/agent.yaml"),
    "counts bytes": (None, "agent.yaml", "# équipe\n", "agent.yaml"),
    "dotdot inside": (None, "team/../agent.yaml", POLICY, "agent.yaml"),
    "linked root": (linked_root, "agent.yaml", POLICY, "agent.yaml"),
    "alias inside": (alias_inside, "alias.yaml", POLICY, "real.yaml"),
    "dot root": (root_in_dot_directory, "agent.yaml", POLICY, "agent.yaml"),
    "yml": (None, "agent.yml", POLICY, "agent.yml"),
    "250-byte name": (None, "n" * 245 + ".yaml", POLICY, "n" * 245 + ".yaml"),
    "one byte": (None, "agent.yaml", "x", "agent.yaml"),
    "at the cap": (None, "agent.yaml", "x" * CAP, "agent.yaml"),
}


@pytest.mark.parametrize("setup, path, content, written", WRITES.values(), ids=WRITES)
async def test_policy_write_local_happy_path(
    root, tmp_path, setup, path, content, written
):
    target_root = setup(root, tmp_path) if setup else root

    text = await write(target_root, path, content)

    assert (target_root / written).read_text(encoding="utf-8") == content
    size = len(content.encode())
    assert text == f"wrote {written} ({size} {'byte' if size == 1 else 'bytes'})"
    if setup is alias_inside:
        assert (root / "alias.yaml").is_symlink()


def links_out(root, tmp_path):
    (tmp_path / "outside").mkdir()
    (root / "out").symlink_to(tmp_path / "outside")
    (root / "escape.yaml").symlink_to(tmp_path / "outside" / "x.yaml")


def alias_to_dot_path(root, tmp_path):
    (root / ".git").mkdir()
    (root / "alias.yaml").symlink_to(root / ".git" / "config.yaml")


def directory_target(root, tmp_path):
    (root / "team.yaml").mkdir()


def repo_inside(root, tmp_path):
    # A multi-repo parent such as ~/workspace: start-up allows it, writes into
    # one of its repos are refused.
    (root / "hexgate" / ".git").mkdir(parents=True)


LEAVES, NOT_POLICY, OK = "not a file inside", "not a policy file", "policy:write"
NOT_POLICIES = """Makefile agent.yaml.bak agent.json .git/hooks/pre-commit .hexgate.yaml
    .config/agent.yaml team/.hidden/agent.yaml""".split()
# (setup, path, content, scopes, error)
REFUSALS = {
    **{
        f"leaves {p!r}": (None, p, POLICY, OK, LEAVES)
        for p in ["../x", "team/../../x", "/etc/x", "", ".", "team/.."]
    },
    **{
        f"links out {p}": (links_out, p, POLICY, OK, LEAVES)
        for p in ["out/x.yaml", "escape.yaml"]
    },
    **{f"not policy {p}": (None, p, POLICY, OK, NOT_POLICY) for p in NOT_POLICIES},
    "alias to dot path": (alias_to_dot_path, "alias.yaml", POLICY, OK, NOT_POLICY),
    "read-only scope": (
        existing,
        "agent.yaml",
        POLICY,
        "policy:read",
        "'policy:write'",
    ),
    "lone surrogate": (None, "agent.yaml", "\ud800", OK, "could not write"),
    "NUL in path": (None, "a\x00.yaml", POLICY, OK, "could not write"),
    "over the cap": (existing, "agent.yaml", "x" * (CAP + 1), OK, "is over"),
    "over in bytes": (None, "agent.yaml", "é" * (CAP // 2 + 1), OK, "is over"),
    "directory target": (directory_target, "team.yaml", POLICY, OK, "'team.yaml'"),
    **{
        f"in a repo {p}": (repo_inside, p, POLICY, OK, "inside the repo hexgate")
        for p in ["hexgate/deploy.yml", "hexgate/platform/compose.yaml"]
    },
}


@pytest.mark.parametrize(
    "setup, path, content, scopes, error", REFUSALS.values(), ids=REFUSALS
)
async def test_when_write_is_refused_then_nothing_changes(
    root, tmp_path, setup, path, content, scopes, error
):
    if setup:
        setup(root, tmp_path)
    before = snapshot(tmp_path)

    with pytest.raises(ToolError, match=error):
        await write(root, path, content, scopes)

    assert snapshot(tmp_path) == before


def snapshot(directory):
    """Every path below `directory` with its mode and, for a file, its bytes."""
    return {
        p: (
            p.lstat().st_mode,
            p.read_bytes() if p.is_file() and not p.is_symlink() else None,
        )
        for p in directory.rglob("*")
    }


# 002 pins the 0644 base (0666 would give 0664) and 027 the umask itself; a
# replaced file keeps its bits, except setuid, setgid, sticky and execute.
@pytest.mark.parametrize(
    "old_mode, umask, mode",
    [(None, 0o002, 0o644), (None, 0o027, 0o640), (0o640, 0o022, 0o640)]
    + [(0o4777, 0o022, 0o666)],
)
async def test_policy_write_local_sets_the_mode(root, old_mode, umask, mode):
    if old_mode is not None:
        (root / "agent.yaml").write_text("old")
        (root / "agent.yaml").chmod(old_mode)
    previous = os.umask(umask)
    try:
        await write(root, "agent.yaml")
    finally:
        os.umask(previous)

    assert stat.S_IMODE((root / "agent.yaml").stat().st_mode) == mode


async def test_when_rename_fails_then_policy_write_local_keeps_the_old_file(
    root, monkeypatch
):
    (root / "agent.yaml").write_text("old")

    full = Mock(side_effect=OSError(28, "No space left on device"))
    monkeypatch.setattr(write_local.os, "replace", full)

    with pytest.raises(
        ToolError, match="could not write 'agent.yaml': No space left on device$"
    ):
        await write(root, "agent.yaml")

    assert (root / "agent.yaml").read_text() == "old"
    assert os.listdir(root) == ["agent.yaml"]


def test_when_a_directory_is_swapped_for_a_link_then_the_write_refuses(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "team").mkdir()
    target = write_local.confine(root, "team/x.yaml")
    shutil.rmtree(root / "team")
    (root / "team").symlink_to(outside)

    with pytest.raises(ToolError, match="changed while writing"):
        write_local.write_atomic(root, target, b"x", 0o644)

    assert os.listdir(outside) == []


async def test_policy_write_local_writes_off_the_event_loop(root, monkeypatch):
    threads = []
    write_atomic = write_local.write_atomic

    def spy(*args):
        threads.append(threading.current_thread())
        write_atomic(*args)

    monkeypatch.setattr(write_local, "write_atomic", spy)

    await write(root, "agent.yaml")

    assert threads and threads[0] is not threading.current_thread()


def list_tools(settings, key) -> str:
    respx.get(JWKS_URL).respond(json=jwks(key))
    with mcp_client(settings) as c:
        response = post_mcp(c, LIST_TOOLS, key.mint())
    return response.text


@respx.mock
def test_when_local_policy_dir_is_unset_then_policy_write_local_is_absent(key):
    text = list_tools(SETTINGS, key)

    # The listing itself came back: the absence below is not an empty reply.
    assert '"policy_reference"' in text
    assert '"policy_write_local"' not in text


@respx.mock
def test_when_local_policy_dir_is_set_then_policy_write_local_is_listed(key, root):
    settings = dataclasses.replace(SETTINGS, local_policy_dir=root)

    data = next(
        line.removeprefix("data: ")
        for line in list_tools(settings, key).splitlines()
        if line.startswith("data: ")
    )
    tools = {t["name"]: t for t in json.loads(data)["result"]["tools"]}

    assert tools["policy_write_local"]["annotations"]["destructiveHint"] is True


async def test_when_root_is_gone_then_policy_write_local_recreates_it(tmp_path, caplog):
    root = tmp_path / "parent" / "policies"
    root.mkdir(parents=True)
    shutil.rmtree(tmp_path / "parent")

    with caplog.at_level(logging.INFO, logger="hexgate_mcp.tools.write_local"):
        await write(root, "team/agent.yaml")

    assert (root / "team" / "agent.yaml").read_text() == POLICY
    assert "recreated the local policy directory" in caplog.text


async def test_when_root_fails_its_checks_mid_run_then_policy_write_local_refuses(
    root,
):
    shutil.rmtree(root)
    root.write_text("")

    with pytest.raises(ToolError, match="not a directory"):
        await write(root, "agent.yaml")

    assert root.read_text() == ""


async def test_when_a_write_into_a_recreated_root_fails_then_none_is_left(
    tmp_path, monkeypatch
):
    root = tmp_path / "parent" / "policies"
    full = Mock(side_effect=OSError(28, "No space left on device"))
    monkeypatch.setattr(write_local.os, "replace", full)

    with pytest.raises(ToolError, match="No space left"):
        await write(root, "team/agent.yaml")

    assert os.listdir(tmp_path) == []


async def test_when_a_nested_write_fails_then_no_new_directory_is_left(
    root, monkeypatch
):
    (root / "org").mkdir()
    full = Mock(side_effect=OSError(28, "No space left on device"))
    monkeypatch.setattr(write_local.os, "replace", full)

    with pytest.raises(ToolError, match="No space left"):
        await write(root, "org/team/sub/agent.yaml")

    assert os.listdir(root) == ["org"] and os.listdir(root / "org") == []


def test_when_writes_race_into_a_new_directory_then_both_land(root):
    for run in range(20):
        barrier = threading.Barrier(2)
        targets = [root / f"r{run}" / "team" / f"{name}.yaml" for name in "ab"]

        def write_one(target):
            barrier.wait()
            write_local.write_atomic(root, target, b"x", 0o644)

        threads = [threading.Thread(target=write_one, args=(t,)) for t in targets]
        [t.start() for t in threads]
        [t.join() for t in threads]

        assert all(t.read_bytes() == b"x" for t in targets)


async def test_policy_write_local_fsyncs_the_directory_entry(root, monkeypatch):
    synced = []
    fsync = os.fsync
    monkeypatch.setattr(
        write_local.os,
        "fsync",
        lambda fd: synced.append(stat.S_ISDIR(os.fstat(fd).st_mode)) or fsync(fd),
    )

    await write(root, "agent.yaml")

    assert synced == [False, True]
