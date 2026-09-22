from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Iterable
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from typing import Any, TypeVar

from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from hexgate.manifest.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    SkillDefinition,
    SkillResources,
    ToolDefinition,
)

_log = logging.getLogger(__name__)

_T = TypeVar("_T")

# Registration snapshot: no session state, so no state-derived tools.
_NO_CONTEXT = None

# Accessed structurally rather than by isinstance: SkillToolset does not exist
# below ADK 1.25.0 and hexgate supports >=1.14, so importing it at module scope
# would break the pinned floor.
_SKILLS_ATTR = "_skills"
_PROVIDED_TOOLS_ATTR = "_provided_tools_by_name"
_PROVIDED_TOOLSETS_ATTR = "_provided_toolsets"
_ADDITIONAL_TOOLS_KEY = "adk_additional_tools"

# Public on BaseToolset since 1.14, and assignable after construction, so it
# applies on every supported ADK regardless of whether SkillToolset's own
# constructor forwards it (which it does only from 2.4.0).
_PREFIX_ATTR = "tool_name_prefix"


def create_google_manifest(
    agent: Agent, *, description: str | None = None
) -> AgentManifest:
    """Build an AgentManifest from a Google ADK agent."""

    tools: list[ToolDefinition] = []
    skills: list[SkillDefinition] = []
    for entry in agent.tools:
        if isinstance(entry, BaseToolset):
            skills.extend(_collect_skills(entry))
        for tool in _expand_entry(entry):
            definition = _to_tool_definition(tool)
            if definition is not None:
                tools.append(definition)

    tools.extend(_additional_tool_definitions(agent.tools, skills, seen=tools))

    # resolve description from agent or provided description
    description = description or agent.description or None

    return AgentManifest(
        name=agent.name,
        description=description,
        framework=AgentFramework.GOOGLE,
        model=_extract_model(agent.model),
        system_prompt=_extract_system_prompt(agent.instruction),
        tools=tools,
        # None, not [], so an agent without skills serializes — and therefore
        # hashes — exactly as it did before this field existed.
        skills=skills or None,
    )


def _run_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run ``coro`` to completion from sync code, loop or no loop.

    ``create_manifest`` is sync and normally called from the CLI, but nothing
    stops a caller invoking it from inside a running loop, where ``asyncio.run``
    raises. Fall back to a worker thread with its own loop rather than failing
    registration.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        in_running_loop = False
    else:
        in_running_loop = True
    # Deliberately outside the except block: running the coroutine there would
    # chain any failure onto the "no running event loop" probe, so the warning
    # below would lead with that instead of the real cause.
    if not in_running_loop:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def expand_toolset(toolset: BaseToolset) -> list[BaseTool]:
    """The tools a toolset declares, for a registration-time snapshot.

    Uses the prefixed variant so the manifest records the names the model will
    call. Passing no ReadonlyContext is deliberate: a toolset resolves
    state-dependent tools only when given one, so this returns the static
    surface and never touches session state.
    """
    try:
        return _run_sync(toolset.get_tools_with_prefix(_NO_CONTEXT))
    except Exception:
        # A toolset that cannot enumerate offline (a live MCP connection, say)
        # must not break registration: the agent still registers, minus that
        # toolset's tools.
        _log.warning(
            "could not expand toolset %s; its tools are absent from the manifest",
            type(toolset).__name__,
            exc_info=True,
        )
        return []


def _expand_entry(entry: object) -> list[BaseTool]:
    """Normalize one ``agent.tools`` entry into zero or more BaseTools.

    The toolset branch goes first: a toolset has no ``_get_declaration``, so it
    would otherwise fall through to ``FunctionTool(func=entry)``.
    """
    if isinstance(entry, BaseToolset):
        return expand_toolset(entry)
    if hasattr(entry, "_get_declaration"):
        return [entry]  # type: ignore[list-item]
    return [FunctionTool(func=entry)]


def _skill_objects(entry: object) -> list[Any]:
    """The Skill objects a toolset holds, or [] for any other toolset.

    Structural rather than isinstance: SkillToolset does not exist below ADK
    1.25.0, and hexgate supports >=1.14. A toolset that stops exposing
    ``_skills`` degrades to "no skills in the manifest", never an exception.
    """
    skills = getattr(entry, _SKILLS_ATTR, None)
    return list(skills.values()) if isinstance(skills, dict) else []


def _collect_skills(entry: object) -> list[SkillDefinition]:
    """Skill definitions from a skills toolset; [] for any other toolset."""
    return [_to_skill_definition(skill) for skill in _skill_objects(entry)]


def _to_skill_definition(skill: Any) -> SkillDefinition:
    """Map one ADK Skill onto the manifest shape.

    ``source`` stays None: a SkillToolset is built from already-loaded Skill
    objects and retains no library path to record.

    Resource names are sorted because ADK enumerates them with an unsorted
    ``rglob``, so their order is whatever the filesystem returns. That order
    would otherwise reach content_hash — re-registering the same skill from
    another machine would mint a fresh AgentVersion — and, above
    MAX_RESOURCES_PER_SKILL, decide *which* names survive truncation.
    """
    frontmatter = skill.frontmatter
    return SkillDefinition(
        name=skill.name,
        description=skill.description,
        resources=SkillResources(
            references=sorted(skill.resources.list_references()),
            assets=sorted(skill.resources.list_assets()),
            scripts=sorted(skill.resources.list_scripts()),
        ),
        allowed_tools=_parse_allowed_tools(frontmatter.allowed_tools),
        additional_tools=_parse_additional_tools(frontmatter.metadata),
        content_hash=sha256(skill.instructions.encode("utf-8")).hexdigest(),
    )


def _parse_allowed_tools(value: object) -> list[str]:
    """Split the agentskills.io ``allowed-tools`` frontmatter.

    A space-delimited string, not a list — and bare ``.split()`` so runs of
    whitespace collapse and an absent value yields [] rather than [""].
    """
    return value.split() if isinstance(value, str) else []


def _parse_additional_tools(metadata: object) -> list[str]:
    """Tool names from ``metadata.adk_additional_tools``.

    ADK validates the key to be a list only when present, and ``extra="allow"``
    on Frontmatter lets arbitrary YAML reach this dict, so coerce defensively.
    """
    if not isinstance(metadata, dict):
        return []
    names = metadata.get(_ADDITIONAL_TOOLS_KEY)
    if not isinstance(names, list):
        return []
    return [name for name in names if isinstance(name, str)]


def _provided_tools(entry: object) -> dict[str, BaseTool]:
    """Tools a skills toolset was constructed with, keyed as a skill names them.

    Both shapes ADK accepts: tools passed individually and tools reached
    through a nested toolset. The key is the name ``adk_additional_tools`` is
    matched against — bare for an individually passed tool, already carrying
    the nested toolset's own prefix for the rest. The outer toolset's prefix is
    applied on top later, exactly as ADK applies it at runtime.
    """
    by_name = getattr(entry, _PROVIDED_TOOLS_ATTR, None)
    provided: dict[str, BaseTool] = dict(by_name) if isinstance(by_name, dict) else {}
    nested = getattr(entry, _PROVIDED_TOOLSETS_ATTR, None)
    for toolset in nested if isinstance(nested, list) else []:
        if isinstance(toolset, BaseToolset):
            provided.update({tool.name: tool for tool in expand_toolset(toolset)})
    return provided


def _callable_name(entry: object, name: str) -> str:
    """The name the model calls a toolset's tool by.

    Mirrors ``BaseToolset.get_tools_with_prefix``, which is ``@final`` and
    rewrites every name it returns to ``f"{prefix}_{tool.name}"``. Additional
    tools are returned from ``get_tools``, so they pass through it too — while
    ``_provided_tools_by_name`` holds them under their bare names. Recording
    the bare name would put an entry in the manifest that no runtime tool
    answers to, and the policy generated from it would deny the prefixed name
    the gate actually sees.
    """
    prefix = getattr(entry, _PREFIX_ATTR, None)
    return f"{prefix}_{name}" if isinstance(prefix, str) and prefix else name


def _additional_tool_definitions(
    entries: Iterable[object],
    skills: list[SkillDefinition],
    *,
    seen: list[ToolDefinition],
) -> list[ToolDefinition]:
    """ToolDefinitions for tools a skill exposes only once activated.

    These are supplied at toolset construction but resolved from session state,
    so they are absent from a registration snapshot. Recording them keeps the
    manifest — and the policy generated from it — aware of the agent's full
    reachable tool surface.

    Iterating ``sorted`` names keeps the manifest, and therefore its
    content_hash, stable across runs. A name with no matching provided tool is
    skipped: ADK itself resolves it to nothing at runtime.
    """
    wanted = {name for skill in skills for name in skill.additional_tools}
    if not wanted:
        return []
    known = {definition.name for definition in seen}
    found: list[ToolDefinition] = []
    for entry in entries:
        provided = _provided_tools(entry)
        if not provided:
            continue
        for name in sorted(wanted):
            # Declared name looks the tool up; callable name is what gets
            # recorded and what de-duplication compares, since that is the
            # namespace every other entry in ``tools`` already lives in.
            callable_name = _callable_name(entry, name)
            if callable_name in known:
                continue
            tool = provided.get(name)
            if tool is None:
                continue
            definition = _to_tool_definition(tool, name=callable_name)
            if definition is not None:
                found.append(definition)
                known.add(callable_name)
    return found


def _extract_model(model: str | BaseLlm) -> str | None:
    """Return the model id for a Google ADK agent.

    Returns None when neither the string form nor ``BaseLlm.model``
    yields an identifier — better than baking a Python class name into
    content_hash and making the hash drift on SDK refactors.
    """
    if isinstance(model, str):
        return model or None
    # BaseLlm exposes the model under .model
    name = getattr(model, "model", None)
    if isinstance(name, str) and name:
        return name
    return None


def _extract_system_prompt(instruction: object) -> str | None:
    """Return the static instruction string for a Google ADK agent.

    ``LlmAgent.instruction`` is ``str | InstructionProvider`` where the
    provider form is a callable resolved per invocation. We only snapshot
    the static string variant.
    """
    if isinstance(instruction, str):
        return instruction or None
    return None


def _to_tool_definition(
    tool: BaseTool, *, name: str | None = None
) -> ToolDefinition | None:
    """Convert a Google ADK tool to a ToolDefinition.

    ``name`` overrides the tool's own, for a tool whose callable name is
    decided by the toolset carrying it rather than by the tool itself.

    Reads schema from BOTH declaration fields: ``parameters`` (the typed
    ``google.genai.types.Schema`` shape ADK's native ``FunctionTool``
    populates from Python signatures) AND ``parametersJsonSchema`` (the
    raw JSON Schema dict path used by tools that came from a source
    without Python type hints — hexgate's MCP wrapper is the canonical
    case). Without the JSON-Schema fallback, every MCP tool registers
    with an empty argument schema and any policy generated from that
    (write-vs-read classification, arg-level rules) is computed
    against zero fields — silently mis-gated.
    """
    declaration = tool._get_declaration()
    if declaration is None:
        return None

    parameters = declaration.parameters
    if parameters is not None:
        raw_properties: dict[str, Any] = dict(parameters.properties or {})
        properties = {
            prop_name: InputProperty(
                title=prop_name,
                type=_schema_type(prop),
            )
            for prop_name, prop in raw_properties.items()
        }
        required = list(parameters.required or [])
    else:
        properties, required = _properties_from_json_schema(
            declaration.parameters_json_schema
        )

    return ToolDefinition(
        name=name or tool.name,
        description=tool.description or "",
        input_schema=InputSchema(properties=properties, required=required),
    )


def _properties_from_json_schema(
    schema: Any,
) -> tuple[dict[str, InputProperty], list[str]]:
    """Extract properties + required list from a raw JSON Schema dict.

    Falls back to empty maps when the schema is missing, malformed, or
    doesn't declare object properties. The registered ToolDefinition
    still carries name + description; only the arg surface goes empty.
    """
    if not isinstance(schema, dict):
        return {}, []
    raw_properties = schema.get("properties")
    if not isinstance(raw_properties, dict):
        raw_properties = {}
    properties = {
        prop_name: InputProperty(
            title=prop_name,
            type=_json_schema_prop_type(prop),
        )
        for prop_name, prop in raw_properties.items()
    }
    raw_required = schema.get("required")
    required = (
        [str(r) for r in raw_required if isinstance(r, str)]
        if isinstance(raw_required, list)
        else []
    )
    return properties, required


def _json_schema_prop_type(prop: Any) -> str:
    """Read the ``type`` field off a raw JSON Schema property dict."""
    if isinstance(prop, dict):
        t = prop.get("type")
        if isinstance(t, str):
            return t.lower()
    return "string"


def _schema_type(schema: Any) -> str:
    """Convert a Google ADK Schema's Type enum to a JSON-Schema type string."""
    type_value = getattr(schema, "type", None)
    if type_value is None:
        return "string"
    raw = getattr(type_value, "value", type_value)
    return str(raw).lower()
