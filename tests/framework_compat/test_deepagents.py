"""Version-compat probe for deepagents.

deepagents has no adapter of its own: ``create_deep_agent`` returns a
``CompiledStateGraph`` that is wrapped through the **langchain** adapter
(``wrap_langchain_agent``). The interesting axis is the langchain /
langgraph versions deepagents pulls in transitively.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.framework_compat import _probe
from tests.framework_compat._probe import ALLOWED_TOOL, DENIED_TOOL, DENY_MARKER
from tests.framework_compat.conftest import AGENT_NAMES

INSTRUCTIONS = "You are a helpful assistant. Use the available tools."

_TOOLS_NODE = "tools"
FRAMEWORK_TOOL = "execute"  # deepagents' arbitrary-shell primitive
# Only runs if the gate is missing; ``echo`` keeps that failure harmless.
HARMLESS_COMMAND = "echo hexgate-probe"

SKILL_NAME = "refunder"
SKILLS_SOURCE = "/skills/"
SKILL_MD = "SKILL.md"
SKILL_ALLOWED_TOOLS = ["read_file", "grep"]
SKILL_MD_TEXT = (
    f"---\nname: {SKILL_NAME}\ndescription: Issue refunds.\n"
    f"allowed-tools: {' '.join(SKILL_ALLOWED_TOOLS)}\n"
    "---\nRefund only after checking the order.\n"
)
# Keys of deepagents' SkillMetadata that hexgate's discovery reads.
SKILL_METADATA_KEYS = {"name", "path", "description", "allowed_tools"}
# How deepagents' skills prompt tells the model where a skill's instructions are.
SHOWN_PATH_PATTERN = re.compile(r"Read `([^`]+)`")

# deepagents has no adapter of its own and rides the installed langchain. If
# that pairing is incompatible (e.g. deepagents importing a symbol a newer
# langchain moved), it fails at import — a genuine matrix result, so skip the
# whole module with the captured reason rather than erroring as a scaffold bug.
try:
    from deepagents import create_deep_agent as _create_deep_agent

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 — surface any import-time incompatibility
    _create_deep_agent = None
    _IMPORT_ERROR = exc

pytestmark = [
    pytest.mark.framework_compat,
    pytest.mark.skipif(
        _IMPORT_ERROR is not None,
        reason=f"deepagents incompatible with installed langchain: {_IMPORT_ERROR!r}",
    ),
]


def _build_tools():
    from langchain_core.tools import tool

    @tool
    def get_weather(city: str) -> str:
        """Return a weather report for a city."""
        _probe.record_execution(ALLOWED_TOOL)
        return f"{city}: sunny, 21C"

    @tool
    def delete_user(user_id: str) -> str:
        """Delete a user account."""
        _probe.record_execution(DENIED_TOOL)
        return f"deleted {user_id}"

    return [get_weather, delete_user]


def _model():
    from langchain_openai import ChatOpenAI

    # Real key when present (Tier 2 e2e); dummy for offline Tier 0/1 construction.
    key = os.environ.get("OPENAI_API_KEY", "sk-probe-dummy")
    return ChatOpenAI(model="gpt-4o-mini", api_key=key)


def _build_graph(tools):
    graph = _create_deep_agent(model=_model(), tools=tools, system_prompt=INSTRUCTIONS)
    # deepagents may leave graph.name unset; resolve_policy needs a non-empty
    # lookup key, so name it explicitly before wrapping.
    if not getattr(graph, "name", None):
        graph.name = AGENT_NAMES["deepagents"]
    return graph


def _build_wrapped(tools):
    from hexgate.adapters.langchain import wrap_langchain_agent

    return wrap_langchain_agent(agent=_build_graph(tools), tools=tools)


def _bound_tools(graph: Any) -> dict[str, Any]:
    """The tools actually bound in the graph's ToolNode, by name.

    Read from the framework directly rather than via ``discover_graph_tools``,
    so the probe cannot pass by agreeing with the code under test.
    """
    return graph.nodes[_TOOLS_NODE].bound.tools_by_name


def test_contract():
    """Tier 0 — create_deep_agent returns a langchain-adapter-shaped graph."""
    from langgraph.graph.state import CompiledStateGraph

    tools = _build_tools()
    graph = _create_deep_agent(model=_model(), tools=tools, system_prompt=INSTRUCTIONS)
    assert isinstance(graph, CompiledStateGraph)


def _build_graph_with_skills(tmp_path: Path) -> tuple[Any, Any]:
    """A deep agent with one real skill on disk, and the backend serving it."""
    from deepagents.backends import FilesystemBackend

    skill_dir = tmp_path / SKILLS_SOURCE.strip("/") / SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / SKILL_MD).write_text(SKILL_MD_TEXT)
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    graph = _create_deep_agent(
        model=_model(),
        tools=_build_tools(),
        system_prompt=INSTRUCTIONS,
        backend=backend,
        skills=[SKILLS_SOURCE],
    )
    return graph, backend


def _recover_skills_middlewares(graph: Any) -> list[Any]:
    """Skills middlewares by the traversal ``discover_skills_middlewares`` uses.

    Spelled out rather than called, so the probe pins deepagents' shape instead
    of agreeing with the code under test.
    """
    owners = (
        getattr(getattr(getattr(node, "bound", None), "func", None), "__self__", None)
        for node in graph.nodes.values()
    )
    return [
        owner
        for owner in owners
        if hasattr(owner, "_backend") and hasattr(owner, "sources")
    ]


def test_contract_skills_surfaces(tmp_path: Path) -> None:
    """Tier 0 — the deepagents internals hexgate's skills support reads.

    A failure here means deepagents moved, not that hexgate is wrong: fix the
    reader named in the failing assertion, not the probe.
    """
    from deepagents.middleware.skills import _list_skills

    from hexgate.adapters.langchain.skills import SkillPathIndex
    from hexgate.manifest.langchain import locate_skills

    graph, backend = _build_graph_with_skills(tmp_path)

    # D3 discover_skills_middlewares: create_deep_agent returns no handle to its
    # SkillsMiddleware, so it is recovered from a compiled hook's bound method.
    middlewares = _recover_skills_middlewares(graph)
    assert middlewares, (
        "SkillsMiddleware is no longer reachable via node.bound.func.__self__ — "
        "deepagents skills are silently absent from manifests and ungated"
    )
    middleware = middlewares[0]

    # D3 _skill_sources: the three middleware attributes read by name.
    assert middleware._backend is backend
    assert middleware.sources == [SKILLS_SOURCE]
    assert len(middleware.source_labels) == len(middleware.sources)

    # D3 _iter_listed_skills: the private lister and the metadata keys read.
    metadata = _list_skills(backend, SKILLS_SOURCE)
    assert [meta["name"] for meta in metadata] == [SKILL_NAME]
    assert SKILL_METADATA_KEYS <= metadata[0].keys()
    # Already split: _to_skill_definition list()s it, so a raw string would
    # become single characters.
    assert metadata[0]["allowed_tools"] == SKILL_ALLOWED_TOOLS

    # D3/D4 _content_hashes: the backend download surface. If this breaks, the
    # hash is silently null and content-pinned skills start denying.
    for attr in ("ls", "download_files", "adownload_files"):
        assert callable(getattr(backend, attr, None)), f"backend lost {attr}"
    [response] = backend.download_files([metadata[0]["path"]])
    assert response.path == metadata[0]["path"]
    assert response.error is None
    assert isinstance(response.content, bytes)

    # D4 SkillPathIndex: the path deepagents shows the model must resolve through
    # hexgate's index, or reads fall through to plain read_file gating.
    index = SkillPathIndex.from_locations(locate_skills(middlewares))
    rendered = middleware._format_skills_list(metadata)
    shown = SHOWN_PATH_PATTERN.search(rendered)
    assert shown is not None, "deepagents' skills prompt no longer names a SKILL.md"
    matched = index.match(shown.group(1))
    assert matched is not None, (
        "deepagents no longer shows the model a path hexgate's index recognises — "
        "skill gating is silently inert"
    )
    via, location = matched
    assert via == "instructions"
    assert location.skill_md_path == metadata[0]["path"]


def test_deny_path_blocks_and_does_not_execute(probe_context):
    """Tier 1 — the denied tool's guarded func returns the structured error."""
    tools = _build_tools()
    _build_wrapped(tools)  # installs enforcer on `tools` in place
    denied = next(t for t in tools if t.name == DENIED_TOOL)
    with probe_context.sync_scope():
        result = denied.func(user_id="u1")
    assert isinstance(result, dict) and result.get("ok") is False
    assert DENY_MARKER in str(result.get("error"))
    assert not _probe.was_executed(DENIED_TOOL)


def test_allow_decision(probe_context):
    """Tier 1 — the resolved policy allows the allowed tool."""
    tools = _build_tools()
    wrapped = _build_wrapped(tools)
    with probe_context.sync_scope():
        decision = wrapped._binding.enforcer.decide(ALLOWED_TOOL, {"city": "Paris"})
    assert decision.allowed


# Names carry a ``_TIER_BY_FRAGMENT`` fragment (scripts/framework_matrix.py) so
# the matrix driver classifies them; an unmapped failure never reads BROKEN.
# Top-level ToolNode only: the ``task`` sub-agent's own ``execute`` is not
# reached by wrapping (see ``discover_graph_tools``).


def test_contract_graph_binds_more_tools_than_the_caller_passed() -> None:
    """Tier 0 — the framework injects tools the caller never passes (#249)."""
    tools = _build_tools()
    bound = _bound_tools(_build_graph(tools))
    assert FRAMEWORK_TOOL in bound
    # Loose on purpose: deepagents' built-in set churns across releases.
    assert len(bound) > len(tools)


def _wrapped_framework_tool() -> Any:
    from hexgate.adapters.langchain import wrap_langchain_agent

    tools = _build_tools()
    graph = _build_graph(tools)
    wrap_langchain_agent(agent=graph, tools=tools)
    return _bound_tools(graph)[FRAMEWORK_TOOL]


def _assert_denied(result: Any) -> None:
    assert isinstance(result, dict) and result.get("ok") is False
    assert DENY_MARKER in str(result.get("error"))


def test_deny_path_wrap_gates_a_framework_injected_tool(probe_context: Any) -> None:
    """Tier 1 — wrapping installs the enforcer on a tool the caller never passed."""
    injected = _wrapped_framework_tool()
    assert getattr(injected, "_hexgate_enforcer_installed", False) is True


def test_deny_path_framework_injected_tool_does_not_execute(
    probe_context: Any,
) -> None:
    """Tier 1 — the injected shell tool's sync gate falls through to default-deny."""
    execute = _wrapped_framework_tool()
    with probe_context.sync_scope():
        _assert_denied(execute.func(command=HARMLESS_COMMAND))


async def test_deny_path_framework_injected_tool_does_not_execute_async(
    probe_context: Any,
) -> None:
    """Tier 1 — same, through the coroutine a real ``ainvoke`` run takes."""
    execute = _wrapped_framework_tool()
    async with probe_context:
        _assert_denied(await execute.coroutine(command=HARMLESS_COMMAND))


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="Tier 2 e2e needs OPENAI_API_KEY"
)
async def test_e2e_allow_executes(probe_context):
    """Tier 2 — a full deep-agent run drives the seam and runs the allowed tool."""
    tools = _build_tools()
    wrapped = _build_wrapped(tools)
    await wrapped.ainvoke(
        {"messages": [{"role": "user", "content": "Weather in Tokyo?"}]},
        hexgate_context=probe_context,
    )
    assert _probe.was_executed(ALLOWED_TOOL)
