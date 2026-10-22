import dataclasses
import os
import stat

import pytest
import respx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.tools import write_local
from tests.conftest import JWKS_URL, SETTINGS, jwks, mcp_client, post_mcp

LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
POLICY = "version: 1\nconstraints: []\n"


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "policies"
    root.mkdir()
    return root


async def write(root, path: str, content: str = POLICY) -> str:
    mcp = FastMCP("test")
    write_local.register(mcp, root)
    result = await mcp.call_tool(
        "policy_write_local", {"path": path, "content": content}
    )
    return result[0].text


async def test_policy_write_local_happy_path(root):
    text = await write(root, "agent.yaml")

    assert (root / "agent.yaml").read_text() == POLICY
    assert text == f"wrote agent.yaml ({len(POLICY)} bytes)"


async def test_when_file_exists_then_policy_write_local_replaces_it(root):
    (root / "agent.yaml").write_text("old")

    await write(root, "agent.yaml")

    assert (root / "agent.yaml").read_text() == POLICY
    # Only the file: the temp file was renamed onto it, not left beside it.
    assert os.listdir(root) == ["agent.yaml"]


async def test_when_file_exists_then_policy_write_local_keeps_its_mode(root):
    (root / "agent.yaml").write_text("old")
    (root / "agent.yaml").chmod(0o640)

    await write(root, "agent.yaml")

    assert stat.S_IMODE((root / "agent.yaml").stat().st_mode) == 0o640


# 002 pins the 0644 base (0666 would give 0664); 027 pins the umask itself.
@pytest.mark.parametrize("umask, mode", [(0o002, 0o644), (0o027, 0o640)])
async def test_when_file_is_new_then_policy_write_local_applies_the_umask(
    root, umask, mode
):
    previous = os.umask(umask)
    try:
        await write(root, "agent.yaml")
    finally:
        os.umask(previous)

    assert stat.S_IMODE((root / "agent.yaml").stat().st_mode) == mode


async def test_when_subdirectory_is_missing_then_policy_write_local_creates_it(root):
    await write(root, "org/team/agent.yaml")

    assert (root / "org" / "team" / "agent.yaml").read_text() == POLICY


async def test_when_content_is_not_ascii_then_policy_write_local_counts_bytes(root):
    text = await write(root, "agent.yaml", "# équipe\n")

    assert (root / "agent.yaml").read_text(encoding="utf-8") == "# équipe\n"
    assert text == "wrote agent.yaml (10 bytes)"


async def test_when_writing_then_the_temp_file_sits_beside_the_target(
    root, monkeypatch
):
    # A rename is atomic only within one file system: never from /tmp.
    renames = []
    replace = os.replace
    monkeypatch.setattr(
        write_local.os,
        "replace",
        lambda src, dst: renames.append(src) or replace(src, dst),
    )

    await write(root, "team/agent.yaml")

    assert [os.path.dirname(src) for src in renames] == [str(root / "team")]


async def test_when_dotdot_stays_inside_then_policy_write_local_writes(root):
    text = await write(root, "team/../agent.yaml")

    assert (root / "agent.yaml").read_text() == POLICY
    assert text.startswith("wrote agent.yaml ")


async def test_when_root_is_a_symlink_then_policy_write_local_writes_through_it(
    root, tmp_path
):
    link = tmp_path / "link"
    link.symlink_to(root)

    await write(link, "agent.yaml")

    assert (root / "agent.yaml").read_text() == POLICY


async def test_when_target_is_a_symlink_inside_then_policy_write_local_writes_its_file(
    root,
):
    (root / "real.yaml").write_text("old")
    (root / "alias.yaml").symlink_to(root / "real.yaml")

    await write(root, "alias.yaml")

    assert (root / "real.yaml").read_text() == POLICY
    assert (root / "alias.yaml").is_symlink()


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "team/../../x",
        "/etc/x",
        "",
        ".",
        "team/..",
    ],
)
async def test_when_path_leaves_the_directory_then_policy_write_local_refuses(
    root, path
):
    with pytest.raises(ToolError, match="not a file inside"):
        await write(root, path)

    assert not (root.parent / "x").exists()
    assert os.listdir(root) == []


@pytest.mark.parametrize("path", ["out/x.yaml", "escape.yaml"])
async def test_when_a_symlink_leads_out_then_policy_write_local_refuses(
    root, tmp_path, path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "out").symlink_to(outside)
    (root / "escape.yaml").symlink_to(outside / "x.yaml")

    with pytest.raises(ToolError, match="not a file inside"):
        await write(root, path)

    assert os.listdir(outside) == []


async def test_when_rename_fails_then_policy_write_local_keeps_the_old_file(
    root, monkeypatch
):
    (root / "agent.yaml").write_text("old")

    def fail(*_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(write_local.os, "replace", fail)

    with pytest.raises(ToolError, match="No space left on device"):
        await write(root, "agent.yaml")

    assert (root / "agent.yaml").read_text() == "old"
    assert os.listdir(root) == ["agent.yaml"]


async def test_when_target_is_a_directory_then_policy_write_local_refuses(root):
    (root / "team").mkdir()

    with pytest.raises(ToolError, match="could not write 'team'"):
        await write(root, "team")

    assert os.listdir(root / "team") == []


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

    text = list_tools(settings, key)

    assert '"policy_write_local"' in text
    assert '"destructiveHint":true' in text
