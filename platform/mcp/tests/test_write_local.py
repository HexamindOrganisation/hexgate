import dataclasses
import json
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
    "read-only scope": (None, "agent.yaml", POLICY, "policy:read", "'policy:write'"),
    "lone surrogate": (None, "agent.yaml", "\ud800", OK, "could not write"),
    "NUL in path": (None, "a\x00.yaml", POLICY, OK, "could not write"),
    "over the cap": (None, "agent.yaml", "x" * (CAP + 1), OK, "is over"),
    "over in bytes": (None, "agent.yaml", "é" * (CAP // 2 + 1), OK, "is over"),
    "directory target": (directory_target, "team.yaml", POLICY, OK, "'team.yaml'"),
}


@pytest.mark.parametrize(
    "setup, path, content, scopes, error", REFUSALS.values(), ids=REFUSALS
)
async def test_when_write_is_refused_then_nothing_changes(
    root, tmp_path, setup, path, content, scopes, error
):
    if setup:
        setup(root, tmp_path)
    before = sorted(tmp_path.rglob("*"))

    with pytest.raises(ToolError, match=error):
        await write(root, path, content, scopes)

    assert sorted(tmp_path.rglob("*")) == before


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
        write_local.write_atomic(target, b"x", 0o644)

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
