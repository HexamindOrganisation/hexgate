from __future__ import annotations

import importlib
import logging
from hashlib import sha256
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from hexgate.manifest.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    SkillDefinition,
    ToolDefinition,
)

_log = logging.getLogger(__name__)

_TOOLS_NODE = "tools"

# deepagents SkillsMiddleware internals — deepagents is not a dependency, so the
# middleware is read duck-typed and every access is guarded.
_BACKEND_ATTR = "_backend"
_SOURCES_ATTR = "sources"
_SOURCE_LABELS_ATTR = "source_labels"
_DOWNLOAD_FILES_ATTR = "download_files"
_SKILLS_MODULE = "deepagents.middleware.skills"
_LIST_SKILLS_FN = "_list_skills"


def create_langchain_manifest(
    graph: CompiledStateGraph,
    tools: list[BaseTool],
    *,
    description: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    skills_middleware: object | None = None,
) -> AgentManifest:
    """Build an AgentManifest from a LangChain/LangGraph agent.

    ``model`` and ``system_prompt`` are passed explicitly because compiled
    LangGraph graphs do not reliably expose them after compilation. ``tools`` is
    unioned with the graph's bound tools, so middleware-injected tools are
    recorded too. ``skills_middleware`` is a deepagents ``SkillsMiddleware``
    whose skills are recorded; typed ``object`` so deepagents stays optional.
    """
    agent_name = getattr(graph, "name", None)
    if agent_name is None:
        raise ValueError(
            "LangChain graph has no name — set a name on the graph so the "
            "manifest can identify it on the platform."
        )
    all_tools = _union_tools(tools, discover_graph_tools(graph))
    skills = (
        _collect_deepagents_skills(skills_middleware)
        if skills_middleware is not None
        else []
    )
    return AgentManifest(
        name=agent_name,
        description=description,
        framework=AgentFramework.LANGCHAIN,
        model=model,
        system_prompt=system_prompt,
        tools=[_to_tool_definition(t) for t in all_tools],
        # None, not [], when there are none: content_hash uses exclude_none.
        skills=skills or None,
    )


def discover_graph_tools(graph: CompiledStateGraph) -> list[BaseTool]:
    """Tools bound in a compiled graph's ToolNode, including middleware-injected ones.

    Reads LangGraph internals with no stability promise, so every hop is guarded
    and a graph without a recognisable tools node yields ``[]``. Top level only:
    tools bound inside a sub-agent graph reached through a tool (deepagents'
    ``task``) are neither discovered nor gated here.
    """
    nodes = getattr(graph, "nodes", None)
    node = nodes.get(_TOOLS_NODE) if isinstance(nodes, dict) else None
    bound = getattr(node, "bound", None)
    by_name = getattr(bound, "tools_by_name", None)
    return list(by_name.values()) if isinstance(by_name, dict) else []


def _union_tools(
    explicit: list[BaseTool], discovered: list[BaseTool]
) -> list[BaseTool]:
    """Caller's tools first, then discovered tools not already named.

    Order is deterministic because the manifest's tool list feeds its content hash.
    """
    merged = list(explicit)
    seen = {tool.name for tool in merged}
    for tool in discovered:
        if tool.name not in seen:
            merged.append(tool)
            seen.add(tool.name)
    return merged


def _collect_deepagents_skills(middleware: object) -> list[SkillDefinition]:
    """Skills a deepagents SkillsMiddleware exposes, mapped onto the manifest.

    Later sources win on a name collision, matching deepagents' own layering.
    """
    backend, sources = _skill_sources(middleware)
    if backend is None:
        return []
    by_name: dict[str, tuple[dict[str, Any], str]] = {}
    for path, label in sources:
        for meta in _list_skill_metadata(backend, path):
            by_name[meta["name"]] = (meta, label)
    hashes = _content_hashes(backend, [meta["path"] for meta, _ in by_name.values()])
    return [
        _to_skill_definition(meta, label, hashes.get(meta["path"]))
        for meta, label in by_name.values()
    ]


def _skill_sources(middleware: object) -> tuple[object | None, list[tuple[str, str]]]:
    """``(backend, [(path, label), …])`` from a SkillsMiddleware, or ``(None, [])``.

    Any missing internal degrades to "no skills discovered", never an exception:
    a manifest without skills is recoverable, a register that cannot run is not.
    A path without an aligned ``source_labels`` entry is its own label.
    """
    backend = getattr(middleware, _BACKEND_ATTR, None)
    paths = getattr(middleware, _SOURCES_ATTR, None)
    if backend is None or not isinstance(paths, (list, tuple)):
        _log.warning(
            "skills_middleware exposes no backend or sources; no skills recorded"
        )
        return None, []
    labels = getattr(middleware, _SOURCE_LABELS_ATTR, None)
    if not isinstance(labels, (list, tuple)) or len(labels) != len(paths):
        labels = paths
    return backend, [
        (path, str(label))
        for path, label in zip(paths, labels, strict=True)
        if isinstance(path, str)
    ]


def _list_skill_metadata(backend: object, source_path: str) -> list[dict[str, Any]]:
    """deepagents' own skill listing for one source, or [] if unavailable.

    Delegates to deepagents' private parser so name validation and frontmatter
    semantics cannot drift from what the agent itself loads.
    """
    try:
        list_skills = getattr(importlib.import_module(_SKILLS_MODULE), _LIST_SKILLS_FN)
    except Exception:  # noqa: BLE001 — deepagents absent or restructured
        _log.warning("deepagents skill listing unavailable; no skills recorded")
        return []
    try:
        return list(list_skills(backend, source_path))
    except Exception:  # noqa: BLE001
        _log.warning(
            "could not list deepagents skills under %r; none recorded",
            source_path,
            exc_info=True,
        )
        return []


def _content_hashes(backend: object, paths: list[str]) -> dict[str, str]:
    """sha256 of each SKILL.md body, keyed by path, in one batched download.

    A path whose body cannot be read is omitted rather than hashed as empty.
    """
    download = getattr(backend, _DOWNLOAD_FILES_ATTR, None)
    if download is None or not paths:
        return {}
    try:
        responses = download(paths)
    except Exception:  # noqa: BLE001
        _log.warning("could not read skill bodies; hashes omitted", exc_info=True)
        return {}
    hashes: dict[str, str] = {}
    for response in responses:
        content = getattr(response, "content", None)
        if getattr(response, "error", None) or not isinstance(content, bytes):
            continue
        hashes[response.path] = sha256(content).hexdigest()
    return hashes


def _to_skill_definition(
    meta: dict[str, Any], label: str, content_hash: str | None
) -> SkillDefinition:
    """Map one deepagents SkillMetadata onto the manifest shape.

    ``resources`` is None because deepagents discovery is frontmatter-only —
    distinct from ``SkillResources()``, which would claim the skill ships none.
    ``allowed_tools`` arrives already split by deepagents.
    """
    return SkillDefinition(
        name=meta["name"],
        description=meta.get("description") or "",
        source=label,
        resources=None,
        allowed_tools=list(meta.get("allowed_tools") or []),
        content_hash=content_hash,
    )


def _to_tool_definition(tool: BaseTool) -> ToolDefinition:
    """Convert a LangChain/LangGraph tool to a ToolDefinition."""
    schema = _tool_schema(tool)
    properties = {
        prop_name: InputProperty(
            title=prop.get("title", prop_name),
            type=prop.get("type", "string"),
        )
        for prop_name, prop in schema.get("properties", {}).items()
    }
    return ToolDefinition(
        name=tool.name,
        description=tool.description or "",
        input_schema=InputSchema(
            properties=properties,
            required=list(schema.get("required", [])),
        ),
    )


def _tool_schema(tool: BaseTool) -> dict[str, Any]:
    """Return a JSON schema of the arguments the model supplies.

    Reads ``tool_call_schema`` rather than ``args_schema`` so runtime-injected
    parameters are left out: they are not inputs, and a ``ToolRuntime`` field
    cannot be rendered as JSON schema at all.
    """
    if tool.args_schema is None:
        return {}
    schema = tool.tool_call_schema
    if isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    return {}
