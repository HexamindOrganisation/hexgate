from pathlib import Path

import pytest
import respx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from hexgate_mcp.tools import reference
from tests.conftest import JWKS_URL, jwks, mcp_client, post_mcp
from tests.test_server import initialize

REPO_DOCS = Path(__file__).resolve().parents[3] / "docs" / "policy"
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


async def call(sections: dict[str, str], **arguments: str) -> str:
    mcp = FastMCP("test")
    reference.register(mcp, sections)
    content = await mcp.call_tool("policy_reference", arguments)
    return content[0].text


def test_find_docs_dir_happy_path():
    assert reference.find_docs_dir() == REPO_DOCS


def test_when_package_sits_beside_the_docs_then_find_docs_dir_finds_them(tmp_path):
    # The image layout: /app/hexgate_mcp/tools/reference.py, /app/docs/policy.
    module = tmp_path / "hexgate_mcp" / "tools" / "reference.py"
    module.parent.mkdir(parents=True)
    module.touch()
    (tmp_path / "docs" / "policy").mkdir(parents=True)

    assert reference.find_docs_dir(module) == tmp_path / "docs" / "policy"


async def test_policy_reference_happy_path():
    mcp = FastMCP("test")
    reference.register(mcp)
    content = await mcp.call_tool("policy_reference", {})
    text = content[0].text

    assert text.startswith("Sections")
    for name in {p.stem for p in REPO_DOCS.glob("*.mdx")}:
        assert f"\n- {name}: " in text
    assert "\n- yaml-shape: Policy YAML shape — version, constraints" in text


async def test_when_section_is_known_then_policy_reference_returns_it():
    text = await call(reference.load_sections(REPO_DOCS), section="yaml-shape")

    assert text == (REPO_DOCS / "yaml-shape.mdx").read_text(encoding="utf-8")


@pytest.mark.parametrize("section", ["nope", "yaml-shape.mdx", "../../README"])
async def test_when_section_is_unknown_then_policy_reference_refuses(section):
    with pytest.raises(ToolError, match="unknown section.*yaml-shape"):
        await call(reference.load_sections(REPO_DOCS), section=section)


async def test_when_docs_are_missing_then_policy_reference_refuses(caplog):
    assert reference.load_sections(None) == {}
    with pytest.raises(ToolError, match="not installed"):
        await call({})
    assert "no docs/policy" in caplog.text


def test_index_text_happy_path():
    doc = '---\ntitle: "A: B"\ndescription: d\n---\n\ntitle: body, not frontmatter\n'

    assert reference.index_text({"a": doc}).endswith("\n- a: A: B — d")


def test_when_frontmatter_is_absent_then_index_text_lists_the_name():
    doc = "# no frontmatter\ntitle: body"

    assert reference.index_text({"bare": doc}).endswith("\n- bare:  — ")


@respx.mock
def test_when_initialized_then_instructions_are_sent(key):
    respx.get(JWKS_URL).respond(json=jwks(key))

    with mcp_client() as c:
        response = initialize(c, key.mint())

    assert reference.INSTRUCTIONS in response.text


@respx.mock
def test_when_server_is_built_then_policy_reference_is_listed(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {key.mint()}",
    }

    with mcp_client() as c:
        response = c.post("/mcp", json=LIST_TOOLS, headers=headers)

    assert '"policy_reference"' in response.text
    # The description repeats the instructions for clients that drop them.
    assert reference.INSTRUCTIONS in response.text
    assert '"readOnlyHint":true' in response.text


@respx.mock
def test_when_policy_reference_is_called_then_text_is_sent_once(key):
    respx.get(JWKS_URL).respond(json=jwks(key))
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "policy_reference", "arguments": {}},
    }

    with mcp_client() as c:
        response = post_mcp(c, call, key.mint())

    assert response.status_code == 200
    assert '"isError":false' in response.text
    assert "structuredContent" not in response.text
