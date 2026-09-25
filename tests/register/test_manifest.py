import hashlib
import json
import logging

import pytest

from hexgate.manifest import create_manifest
from hexgate.manifest.google import create_google_manifest
from hexgate.manifest.langchain import create_langchain_manifest
from hexgate.manifest.models import (
    MAX_RESOURCES_PER_SKILL,
    MAX_SKILLS,
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
    SkillDefinition,
    SkillResources,
    ToolDefinition,
)
from hexgate.manifest.native import create_hexgate_manifest
from hexgate.manifest.openai import create_openai_manifest
from hexgate.manifest.pydantic_ai import create_pydantic_ai_manifest
from tests.adapters.google.conftest import _FakeToolset


def test_agent_manifest_schema():
    """Test the schema of the agent manifest."""
    manifest = AgentManifest(
        name="test-agent",
        description="A test agent",
        framework=AgentFramework.HEXGATE,
        tools=[],
    )
    assert manifest.name == "test-agent"
    assert manifest.description == "A test agent"
    assert manifest.framework == AgentFramework.HEXGATE
    assert manifest.tools == []


def test_openai_manifest_schema():
    """Test the schema of the OpenAI manifest."""
    from agents import Agent, function_tool

    @function_tool()
    def example_tool(example_input: str) -> str:
        """A test tool."""
        return f"Hello, {example_input}! This is a test tool."

    agent = Agent(
        name="test-agent",
        instructions="A test agent",
        tools=[example_tool],
    )

    expected_manifest = AgentManifest(
        name="test-agent",
        description="A test agent",
        framework=AgentFramework.OPENAI,
        model=None,
        system_prompt="A test agent",
        tools=[
            ToolDefinition(
                name="example_tool",
                description="A test tool.",
                input_schema=InputSchema(
                    properties={
                        "example_input": InputProperty(
                            title="Example Input",
                            type="string",
                        ),
                    },
                    required=["example_input"],
                ),
            )
        ],
    )
    manifest = create_openai_manifest(agent, description="A test agent")
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest

    manifest = create_manifest(agent, description="A test agent")
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest


def test_google_manifest_schema():
    """Test the schema of the Google ADK manifest."""
    from google.adk.agents import Agent

    def example_tool(example_input: str) -> str:
        """A test tool."""
        return f"Hello, {example_input}! This is a test tool."

    agent = Agent(
        name="test_agent",
        model="gemini-2.0-flash",
        description="A test agent",
        instruction="Greet the user.",
        tools=[example_tool],
    )

    expected_manifest = AgentManifest(
        name="test_agent",
        description="A test agent",
        framework=AgentFramework.GOOGLE,
        model="gemini-2.0-flash",
        system_prompt="Greet the user.",
        tools=[
            ToolDefinition(
                name="example_tool",
                description="A test tool.",
                input_schema=InputSchema(
                    properties={
                        "example_input": InputProperty(
                            title="example_input",
                            type="string",
                        ),
                    },
                    required=["example_input"],
                ),
            )
        ],
    )
    manifest = create_google_manifest(agent)
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest

    manifest = create_manifest(agent, description="A test agent")
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest


def test_google_manifest_extracts_mcp_tool_schema_from_json_schema():
    """Regression for post-review finding: _to_tool_definition used to
    read only declaration.parameters (the typed Schema field). Google
    ADK's native FunctionTool populates parameters from Python
    signatures, but hexgate's MCP wrapper populates parametersJsonSchema
    (raw JSON Schema dict) instead. Result: every MCP tool registered
    with an empty arg schema, and any policy generated from that
    (write-vs-read classification, arg-level rules) was computed
    against zero fields — tools silently mis-gated. Now
    _to_tool_definition also reads parametersJsonSchema and lifts
    properties + required from there when parameters is None."""
    from mcp.types import Tool as MCPTool

    from hexgate.adapters.google.mcp import wrap_mcp_toolset
    from hexgate.manifest.google import _to_tool_definition
    from hexgate.mcp import MCPServerConfig
    from hexgate.mcp.proxy import _build_proxy as build
    from hexgate.mcp.proxy import _ToolsetState

    class _FakeClient:
        def __init__(self, config):
            self.config = config

        async def call_tool(self, *_args, **_kwargs):
            raise AssertionError("not exercised")

    cfg = MCPServerConfig(name="slack", transport="stdio", command="slack-mcp")
    schema = {
        "type": "object",
        "properties": {
            "channel": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["channel", "text"],
    }
    mcp_tool = MCPTool(name="send", description="Post a message", inputSchema=schema)
    proxy = build(_ToolsetState(_FakeClient(cfg)), cfg, mcp_tool)

    class _Stub:
        pass

    stub = _Stub()
    stub.proxies = [proxy]
    [adk_tool] = wrap_mcp_toolset(stub)

    tool_def = _to_tool_definition(adk_tool)
    assert tool_def is not None
    assert tool_def.name == "mcp-slack-send"
    # Both properties surface — pre-fix this was `{}`.
    assert set(tool_def.input_schema.properties.keys()) == {"channel", "text"}
    assert tool_def.input_schema.properties["channel"].type == "string"
    # Required list carries through too — pre-fix this was `[]`.
    assert sorted(tool_def.input_schema.required) == ["channel", "text"]


def _google_toolset(tools, *, raises=False, **kwargs):
    """A minimal BaseToolset whose tool list (or failure) the test controls.

    Hand-rolled rather than ADK's SkillToolset, which only exists from ADK
    1.25.0 while pyproject pins google-adk>=1.14.
    """
    from google.adk.tools.base_toolset import BaseToolset

    class _FakeToolset(BaseToolset):
        async def get_tools(self, readonly_context=None):
            if raises:
                raise RuntimeError("cannot enumerate offline")
            return list(tools)

    return _FakeToolset(**kwargs)


def _google_function_tool(name):
    from google.adk.tools.function_tool import FunctionTool

    def tool(text: str) -> str:
        """A toolset member."""
        return text

    tool.__name__ = name
    return FunctionTool(func=tool)


def _google_agent(tools):
    from google.adk.agents import Agent

    return Agent(
        name="test_agent",
        model="gemini-2.0-flash",
        description="A test agent",
        instruction="Greet the user.",
        tools=tools,
    )


def test_google_manifest_expands_a_toolset():
    """Regression for #248: a BaseToolset entry used to hit
    FunctionTool(func=entry) and raise TypeError, because a toolset has no
    _get_declaration. Its member tools now register alongside plain ones."""

    def plain_tool(text: str) -> str:
        """A plain tool."""
        return text

    toolset = _google_toolset(
        [_google_function_tool("search"), _google_function_tool("fetch")]
    )

    manifest = create_google_manifest(_google_agent([plain_tool, toolset]))

    assert [t.name for t in manifest.tools] == ["plain_tool", "search", "fetch"]


def test_google_manifest_toolset_tools_use_prefixed_names():
    """The manifest records the names the model will call, or a generated
    starter policy would reference names that never appear."""
    toolset = _google_toolset(
        [_google_function_tool("search")], tool_name_prefix="mymcp"
    )

    manifest = create_google_manifest(_google_agent([toolset]))

    assert [t.name for t in manifest.tools] == ["mymcp_search"]


@pytest.mark.asyncio
async def test_google_manifest_expands_a_toolset_inside_a_running_loop():
    """Registration is sync but expansion is async. Called from inside a loop,
    asyncio.run raises, so _run_sync falls back to a worker thread rather than
    failing registration."""
    toolset = _google_toolset([_google_function_tool("search")])

    manifest = create_google_manifest(_google_agent([toolset]))

    assert [t.name for t in manifest.tools] == ["search"]


def test_google_manifest_survives_a_toolset_that_cannot_enumerate(caplog):
    """A toolset that cannot list offline (a live MCP connection, say) must not
    break registration: the agent still registers, minus that toolset's tools."""

    def plain_tool(text: str) -> str:
        """A plain tool."""
        return text

    agent = _google_agent([plain_tool, _google_toolset([], raises=True)])

    with caplog.at_level(logging.WARNING, logger="hexgate.manifest.google"):
        manifest = create_google_manifest(agent)

    assert [t.name for t in manifest.tools] == ["plain_tool"]
    assert "could not expand toolset" in caplog.text


def test_pydantic_ai_manifest_schema():
    """Test the schema of the Pydantic AI manifest."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    agent = Agent(TestModel(), name="test-agent", description="A test agent")

    @agent.tool_plain
    def example_tool(example_input: str) -> str:
        """A test tool."""
        return f"Hello, {example_input}! This is a test tool."

    expected_manifest = AgentManifest(
        name="test-agent",
        description="A test agent",
        framework=AgentFramework.PYDANTIC_AI,
        model="test",
        system_prompt=None,
        tools=[
            ToolDefinition(
                name="example_tool",
                description="A test tool.",
                input_schema=InputSchema(
                    properties={
                        "example_input": InputProperty(
                            title="example_input",
                            type="string",
                        ),
                    },
                    required=["example_input"],
                ),
            )
        ],
    )
    manifest = create_pydantic_ai_manifest(agent)
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest

    manifest = create_manifest(agent, description="A test agent")
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest


def test_langchain_manifest_schema():
    """Test the schema of the LangChain manifest."""
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    @tool
    def example_tool(example_input: str) -> str:
        """A test tool."""
        return f"Hello, {example_input}! This is a test tool."

    builder = StateGraph(dict)
    builder.add_node("noop", lambda state: state)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    graph = builder.compile(name="test-agent")

    expected_manifest = AgentManifest(
        name="test-agent",
        description="A test agent",
        framework=AgentFramework.LANGCHAIN,
        model=None,
        system_prompt=None,
        tools=[
            ToolDefinition(
                name="example_tool",
                description="A test tool.",
                input_schema=InputSchema(
                    properties={
                        "example_input": InputProperty(
                            title="Example Input",
                            type="string",
                        ),
                    },
                    required=["example_input"],
                ),
            )
        ],
    )
    manifest = create_langchain_manifest(
        graph,
        [example_tool],
        description="A test agent",
    )
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest

    manifest = create_manifest(graph, tools=[example_tool], description="A test agent")
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest


def test_hexgate_manifest_schema():
    """Test the schema of the Hexgate manifest (HexgateAgent from create_agent)."""
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    from hexgate.agents.factory import HexgateAgent

    @tool
    def example_tool(example_input: str) -> str:
        """A test tool."""
        return f"Hello, {example_input}! This is a test tool."

    builder = StateGraph(dict)
    builder.add_node("noop", lambda state: state)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    graph = builder.compile(name="test-agent")

    agent = HexgateAgent(
        graph=graph,
        model="test-model",
        tools=[example_tool],
        system_prompt=None,
        name="test-agent",
    )

    expected_manifest = AgentManifest(
        name="test-agent",
        description="A test agent",
        framework=AgentFramework.HEXGATE,
        model="test-model",
        system_prompt=None,
        tools=[
            ToolDefinition(
                name="example_tool",
                description="A test tool.",
                input_schema=InputSchema(
                    properties={
                        "example_input": InputProperty(
                            title="Example Input",
                            type="string",
                        ),
                    },
                    required=["example_input"],
                ),
            )
        ],
    )
    manifest = create_hexgate_manifest(agent, description="A test agent")
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest

    manifest = create_manifest(agent, description="A test agent")
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest


def test_hexgate_manifest_system_message_prompt():
    """SystemMessage system prompts are flattened to their text content."""
    from langchain_core.messages import SystemMessage
    from langgraph.graph import END, START, StateGraph

    from hexgate.agents.factory import HexgateAgent

    builder = StateGraph(dict)
    builder.add_node("noop", lambda state: state)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    graph = builder.compile(name="sm-agent")

    agent = HexgateAgent(
        graph=graph,
        model="test-model",
        tools=[],
        system_prompt=SystemMessage(content="hi"),
        name="sm-agent",
    )

    manifest = create_hexgate_manifest(agent)
    assert manifest.system_prompt == "hi"
    assert manifest.model == "test-model"


def test_openai_manifest_callable_instructions():
    """Callable ``instructions`` is dropped (no static text to snapshot)."""
    from agents import Agent

    agent = Agent(
        name="callable-agent",
        instructions=lambda *_args, **_kwargs: "ignored",
        tools=[],
    )

    manifest = create_openai_manifest(agent)
    assert manifest.system_prompt is None


def test_langchain_manifest_explicit_model_and_prompt():
    """LangChain kwargs flow through ``create_manifest`` to the manifest."""
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(dict)
    builder.add_node("noop", lambda state: state)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    graph = builder.compile(name="lc-agent")

    manifest = create_manifest(
        graph,
        tools=[],
        model="gpt-4o-mini",
        system_prompt="be helpful",
    )
    assert manifest.model == "gpt-4o-mini"
    assert manifest.system_prompt == "be helpful"


def test_pydantic_ai_manifest_static_prompts():
    """Static ``system_prompt`` + ``instructions`` strings are concatenated."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    agent = Agent(
        TestModel(),
        name="prompty-agent",
        system_prompt="part one",
        instructions="part two",
    )

    manifest = create_pydantic_ai_manifest(agent)
    assert manifest.system_prompt == "part one\n\npart two"
    assert manifest.model == "test"


class TestSkillDefinitions:
    """The manifest's skill contract.

    Nothing populates ``skills`` yet — A2 (ADK) and D3 (deepagents) do that.
    These tests pin the shape so the producers and the platform mirror agree.
    """

    @staticmethod
    def _manifest(**overrides: object) -> AgentManifest:
        return AgentManifest(
            name="test-agent",
            description="A test agent",
            framework=AgentFramework.HEXGATE,
            tools=[],
            **overrides,
        )

    def test_manifest_without_skills_serializes_identically(self):
        """The SDK half of the hash-continuity contract. Do not delete.

        This does **not** verify the digest. ``compute_manifest_hash`` runs in
        the platform project, over the platform's mirror of this model, so
        nothing asserted here can reach it — today the platform has no
        ``skills`` field at all and drops it at parse time.

        What this pins is the SDK's side of the bargain: ``skills`` defaults to
        None and never serializes as ``[]``. A ``[]`` default would enter the
        platform's canonical JSON once the mirror lands and change the digest
        of every manifest already registered, minting a redundant
        AgentVersion per agent on the next ``hexgate register``.

        The digest-side guard is a schema parity test in the platform project,
        which cannot live here — separate uv project, and it would be red
        until the mirror exists.
        """
        manifest = self._manifest()

        assert manifest.skills is None
        assert "skills" not in manifest.model_dump(mode="json", exclude_none=True)

    def test_manifest_defaults_skills_to_none(self):
        assert self._manifest().skills is None

    def test_skill_definition_defaults(self):
        skill = SkillDefinition(name="pdf", description="Work with PDF files")

        assert skill.source is None
        assert skill.resources is None
        assert skill.content_hash is None
        assert skill.allowed_tools == []
        assert skill.additional_tools == []

    def test_resources_none_is_distinct_from_empty(self):
        """``None`` means not enumerated; ``SkillResources()`` means none ships."""
        not_enumerated = SkillDefinition(name="pdf", description="d", resources=None)
        enumerated_empty = SkillDefinition(
            name="pdf", description="d", resources=SkillResources()
        )

        assert not_enumerated != enumerated_empty
        assert "resources" not in not_enumerated.model_dump(
            mode="json", exclude_none=True
        )
        assert enumerated_empty.model_dump(mode="json", exclude_none=True)[
            "resources"
        ] == {"references": [], "assets": [], "scripts": []}

    def test_skills_list_is_truncated_at_the_cap(self, caplog):
        skills = [
            SkillDefinition(name=f"skill-{index}", description="d")
            for index in range(MAX_SKILLS + 1)
        ]

        with caplog.at_level(logging.WARNING):
            manifest = self._manifest(skills=skills)

        assert manifest.skills is not None
        assert len(manifest.skills) == MAX_SKILLS
        assert manifest.skills[-1].name == f"skill-{MAX_SKILLS - 1}"
        assert "skills" in caplog.text

    def test_duplicate_skill_names_are_collapsed_last_wins(self, caplog):
        """The platform's UNIQUE (agent_version_id, name) depends on this.

        Frameworks merge skill libraries by concatenation, so overriding a
        bundled skill with a user-level one of the same name arrives here as
        two entries.
        """
        with caplog.at_level(logging.WARNING):
            manifest = self._manifest(
                skills=[
                    SkillDefinition(name="pdf", description="bundled"),
                    SkillDefinition(name="docx", description="bundled"),
                    SkillDefinition(name="pdf", description="user override"),
                ]
            )

        assert manifest.skills is not None
        assert [skill.name for skill in manifest.skills] == ["pdf", "docx"]
        assert manifest.skills[0].description == "user override"
        assert "declared more than once" in caplog.text

    def test_skill_resources_are_truncated_at_the_cap(self, caplog):
        references = [f"ref-{index}.md" for index in range(MAX_RESOURCES_PER_SKILL + 1)]

        with caplog.at_level(logging.WARNING):
            resources = SkillResources(references=references)

        assert len(resources.references) == MAX_RESOURCES_PER_SKILL
        assert resources.references[-1] == f"ref-{MAX_RESOURCES_PER_SKILL - 1}.md"
        assert "skill resources" in caplog.text

    def test_skills_round_trip_through_json(self):
        manifest = self._manifest(
            skills=[
                SkillDefinition(
                    name="pdf",
                    description="Work with PDF files",
                    source="/skills",
                    resources=SkillResources(
                        references=["forms.md"], scripts=["fill.py"]
                    ),
                    allowed_tools=["read_file"],
                    additional_tools=["fill_form"],
                    content_hash="a" * 64,
                ),
                SkillDefinition(name="docx", description="Work with Word files"),
            ]
        )

        restored = AgentManifest.model_validate(json.loads(manifest.model_dump_json()))

        assert restored == manifest

    def test_manifest_with_skills_serializes_the_field(self):
        """Negative control for the hash-continuity guard."""
        manifest = self._manifest(
            skills=[SkillDefinition(name="pdf", description="Work with PDF files")]
        )

        dumped = manifest.model_dump(mode="json", exclude_none=True)

        assert [skill["name"] for skill in dumped["skills"]] == ["pdf"]


# --- sub-agent refs (PR 3) -------------------------------------------------


def _graph(name: str):
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(dict)
    builder.add_node("noop", lambda state: state)
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    return builder.compile(name=name)


def test_manifest_subagents_default_none_and_hash_stable():
    """No sub-agents → subagents is None and excluded from the canonical dump, so
    an agent without sub-agents hashes exactly as before this field existed."""
    manifest = AgentManifest(name="a", framework=AgentFramework.HEXGATE, tools=[])
    assert manifest.subagents is None
    assert "subagents" not in manifest.model_dump(mode="json", exclude_none=True)


def test_create_manifest_populates_subagents_native():
    """A native HexgateAgent's mounted agent-as-tool edges become manifest.subagents."""
    from hexgate.adapters.langchain.tools import SubagentTool
    from hexgate.agents.factory import HexgateAgent
    from hexgate.manifest.models import SubagentRef

    child = type("Child", (), {"name": "billing_bot"})()
    agent = HexgateAgent(
        graph=_graph("support_bot"),
        model="m",
        tools=[
            SubagentTool(
                name="delegate_to_billing_bot",
                description="d",
                child=child,
                target_name="billing_bot",
            )
        ],
        system_prompt=None,
        name="support_bot",
    )
    manifest = create_manifest(agent)
    assert manifest.subagents == [SubagentRef(name="billing_bot", via="tool")]
    # And it now participates in the canonical dump (hash covers it).
    assert "subagents" in manifest.model_dump(mode="json", exclude_none=True)


def test_create_manifest_populates_subagents_openai_handoff():
    """OpenAI handoffs surface as via='handoff' refs through create_manifest."""
    from agents import Agent

    from hexgate.manifest.models import SubagentRef

    agent = Agent(name="parent", handoffs=[Agent(name="billing_bot")])
    manifest = create_manifest(agent)
    assert manifest.subagents == [SubagentRef(name="billing_bot", via="handoff")]


def _fake_skill(
    name="refunder",
    *,
    description="Issue refunds",
    instructions="Do the refund.",
    allowed_tools=None,
    metadata=None,
    references=None,
    assets=None,
    scripts=None,
):
    """An ADK Skill's shape, hand-rolled so the mapping tests run on every
    supported ADK — google.adk.skills only exists from 1.25.0."""

    class _Resources:
        def __init__(self):
            self.references = references or []
            self.assets = assets or []
            self.scripts = scripts or []

        def list_references(self):
            return list(self.references)

        def list_assets(self):
            return list(self.assets)

        def list_scripts(self):
            return list(self.scripts)

    class _Frontmatter:
        def __init__(self):
            self.name = name
            self.description = description
            self.allowed_tools = allowed_tools
            self.metadata = metadata if metadata is not None else {}

    class _Skill:
        def __init__(self):
            self.frontmatter = _Frontmatter()
            self.instructions = instructions
            self.resources = _Resources()
            self.name = name
            self.description = description

    return _Skill()


def _google_skills_toolset(tools, skills, *, provided=None, prefix=None):
    """A toolset carrying ADK's private skill attributes.

    Structural, like the adapter's own read: SkillToolset holds its skills in
    ``_skills`` and its activation-time tools in ``_provided_tools_by_name``.
    """
    toolset = _FakeToolset(tools, tool_name_prefix=prefix)
    toolset._skills = (
        {skill.name: skill for skill in skills} if isinstance(skills, list) else skills
    )
    toolset._provided_tools_by_name = {tool.name: tool for tool in provided or []}
    return toolset


def _activated_skill_context(skill_name, agent_name="test_agent"):
    """A context in which ``skill_name`` is already activated, so a real
    SkillToolset resolves its additional tools from state."""

    class _Context:
        invocation_id = "inv-1"

        def __init__(self):
            self.agent_name = agent_name
            self.state = {f"_adk_activated_skill_{agent_name}": [skill_name]}

    return _Context()


def _write_skill_dir(base):
    """A real on-disk skill library ADK's loader accepts."""
    skill_dir = base / "skills" / "refunder"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "assets").mkdir()
    (skill_dir / "scripts").mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: refunder\n"
        "description: Issue refunds to customers\n"
        "allowed-tools: refund lookup\n"
        "metadata:\n"
        "  adk_additional_tools:\n"
        "    - refund\n"
        "---\n"
        "Refund the order, then confirm.\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "guide.md").write_text("Guide", encoding="utf-8")
    (skill_dir / "assets" / "template.txt").write_text("Template", encoding="utf-8")
    (skill_dir / "scripts" / "run.sh").write_text("echo hi", encoding="utf-8")
    return skill_dir


class TestGoogleSkillDiscovery:
    """ADK skills reaching ``AgentManifest.skills``.

    Mostly hand-rolled fakes, which run on every supported ADK; one case builds
    a genuine ``SkillToolset`` so the structural ``_skills`` read is exercised
    against the real object where it exists.
    """

    @staticmethod
    def _manifest(toolset, *extra_tools):
        return create_google_manifest(_google_agent([*extra_tools, toolset]))

    def test_google_manifest_records_skills(self):
        toolset = _google_skills_toolset(
            [_google_function_tool("load_skill")], [_fake_skill()]
        )

        [skill] = self._manifest(toolset).skills

        assert skill.name == "refunder"
        assert skill.description == "Issue refunds"
        assert skill.content_hash == hashlib.sha256(b"Do the refund.").hexdigest()

    def test_google_manifest_enumerates_skill_resources(self):
        toolset = _google_skills_toolset(
            [],
            [
                _fake_skill(
                    references=["guide.md"],
                    assets=["template.txt"],
                    scripts=["run.sh"],
                )
            ],
        )

        [skill] = self._manifest(toolset).skills

        assert skill.resources.references == ["guide.md"]
        assert skill.resources.assets == ["template.txt"]
        assert skill.resources.scripts == ["run.sh"]

    def test_google_manifest_resources_is_never_none_for_adk(self):
        """ADK enumerates L3, so None here would mean "does not enumerate" — a lie."""
        toolset = _google_skills_toolset([], [_fake_skill()])

        [skill] = self._manifest(toolset).skills

        assert skill.resources == SkillResources()

    def test_allowed_tools_is_split_on_whitespace(self):
        """'allowed-tools' is a space-delimited string, not a list."""
        toolset = _google_skills_toolset(
            [], [_fake_skill(allowed_tools="refund  lookup")]
        )

        [skill] = self._manifest(toolset).skills

        assert skill.allowed_tools == ["refund", "lookup"]

    def test_allowed_tools_absent_yields_empty_list(self):
        toolset = _google_skills_toolset([], [_fake_skill(allowed_tools=None)])

        [skill] = self._manifest(toolset).skills

        assert skill.allowed_tools == []

    def test_additional_tools_recorded_from_metadata(self):
        toolset = _google_skills_toolset(
            [], [_fake_skill(metadata={"adk_additional_tools": ["refund"]})]
        )

        [skill] = self._manifest(toolset).skills

        assert skill.additional_tools == ["refund"]

    def test_additional_tools_appear_in_the_tool_list(self):
        """The under-reporting this guards: a tool resolved from session state
        becomes callable once the skill activates, so the manifest — and the
        policy generated from it — must carry it."""
        toolset = _google_skills_toolset(
            [_google_function_tool("load_skill")],
            [_fake_skill(metadata={"adk_additional_tools": ["refund"]})],
            provided=[_google_function_tool("refund")],
        )

        manifest = self._manifest(toolset)

        assert [t.name for t in manifest.tools] == ["load_skill", "refund"]

    def test_additional_tools_are_not_duplicated(self):
        """A tool the agent already carries directly is not recorded twice."""
        toolset = _google_skills_toolset(
            [_google_function_tool("load_skill")],
            [_fake_skill(metadata={"adk_additional_tools": ["refund"]})],
            provided=[_google_function_tool("refund")],
        )

        manifest = self._manifest(toolset, _google_function_tool("refund"))

        assert [t.name for t in manifest.tools] == ["refund", "load_skill"]

    def test_unresolvable_additional_tool_is_skipped(self):
        """ADK resolves an unmatched name to nothing at runtime, so neither
        raise nor invent a phantom entry."""
        toolset = _google_skills_toolset(
            [_google_function_tool("load_skill")],
            [_fake_skill(metadata={"adk_additional_tools": ["ghost"]})],
        )

        manifest = self._manifest(toolset)

        assert [t.name for t in manifest.tools] == ["load_skill"]
        assert manifest.skills[0].additional_tools == ["ghost"]

    def test_agent_without_skills_leaves_skills_none(self):
        """None, not [] — an empty list would serialize and change the
        content_hash of every ADK manifest already registered."""
        manifest = create_google_manifest(
            _google_agent([_google_function_tool("search")])
        )

        assert manifest.skills is None

    def test_toolset_without_skills_attr_degrades(self):
        """A plain toolset expands its tools and contributes no skills."""
        manifest = self._manifest(_google_toolset([_google_function_tool("search")]))

        assert [t.name for t in manifest.tools] == ["search"]
        assert manifest.skills is None

    def test_skills_attr_of_wrong_type_degrades(self):
        toolset = _google_skills_toolset([_google_function_tool("search")], "nonsense")

        manifest = self._manifest(toolset)

        assert [t.name for t in manifest.tools] == ["search"]
        assert manifest.skills is None

    def test_real_skill_toolset_is_discovered(self, tmp_path):
        """The structural ``_skills`` read, against the genuine object."""
        pytest.importorskip("google.adk.tools.skill_toolset")
        from google.adk.skills import load_skill_from_dir
        from google.adk.tools.skill_toolset import SkillToolset

        def refund(order_id: str) -> str:
            """Refund an order."""
            return order_id

        toolset = SkillToolset(
            [load_skill_from_dir(_write_skill_dir(tmp_path))],
            additional_tools=[refund],
        )

        manifest = self._manifest(toolset)

        [skill] = manifest.skills
        assert skill.name == "refunder"
        assert skill.description == "Issue refunds to customers"
        assert skill.allowed_tools == ["refund", "lookup"]
        assert skill.additional_tools == ["refund"]
        assert skill.resources.references == ["guide.md"]
        assert skill.resources.assets == ["template.txt"]
        assert skill.resources.scripts == ["run.sh"]
        assert skill.source is None
        assert "refund" in {t.name for t in manifest.tools}

    def test_manifest_hash_inputs_are_stable_across_runs(self):
        """Guards the sorted iteration: an unordered tool list would churn the
        content_hash and mint a redundant AgentVersion on every register."""

        def build():
            toolset = _google_skills_toolset(
                [_google_function_tool("load_skill")],
                [
                    _fake_skill(
                        metadata={
                            "adk_additional_tools": ["refund", "lookup", "annotate"]
                        }
                    )
                ],
                provided=[
                    _google_function_tool("refund"),
                    _google_function_tool("lookup"),
                    _google_function_tool("annotate"),
                ],
            )
            return self._manifest(toolset)

        assert build().model_dump() == build().model_dump()
        assert [t.name for t in build().tools] == [
            "load_skill",
            "annotate",
            "lookup",
            "refund",
        ]

    def test_skill_resources_are_sorted(self):
        """ADK enumerates L3 with an unsorted rglob, so the order is whatever
        the filesystem returns. Unsorted, it reaches content_hash — the same
        skill re-registered from another machine would mint a fresh
        AgentVersion — and above the cap it decides which names survive
        truncation, so the manifest would misreport what the skill ships."""
        toolset = _google_skills_toolset(
            [],
            [
                _fake_skill(
                    references=["zeta.md", "alpha.md", "mid.md"],
                    assets=["b.txt", "a.txt"],
                    scripts=["run.sh", "build.sh"],
                )
            ],
        )

        [skill] = self._manifest(toolset).skills

        assert skill.resources.references == ["alpha.md", "mid.md", "zeta.md"]
        assert skill.resources.assets == ["a.txt", "b.txt"]
        assert skill.resources.scripts == ["build.sh", "run.sh"]

    def test_additional_tools_are_recorded_under_the_toolset_prefix(self):
        """get_tools_with_prefix is @final and rewrites every name it returns,
        and additional tools come back through get_tools — so the model calls
        'ops_refund'. Recording the bare name would leave a manifest entry no
        runtime tool answers to, and the generated policy would deny the
        prefixed name the gate actually sees."""
        toolset = _google_skills_toolset(
            [_google_function_tool("load_skill")],
            [_fake_skill(metadata={"adk_additional_tools": ["refund"]})],
            provided=[_google_function_tool("refund")],
            prefix="ops",
        )

        manifest = self._manifest(toolset)

        assert [t.name for t in manifest.tools] == ["ops_load_skill", "ops_refund"]

    def test_additional_tools_keep_the_declared_name_on_the_skill(self):
        """The prefix belongs to how the toolset is mounted, not to the skill:
        ADK matches adk_additional_tools against the unprefixed name."""
        toolset = _google_skills_toolset(
            [],
            [_fake_skill(metadata={"adk_additional_tools": ["refund"]})],
            provided=[_google_function_tool("refund")],
            prefix="ops",
        )

        [skill] = self._manifest(toolset).skills

        assert skill.additional_tools == ["refund"]

    def test_prefixed_additional_tools_are_not_duplicated(self):
        """De-duplication compares callable names, so a tool the agent already
        carries under the prefixed name is not recorded twice."""
        toolset = _google_skills_toolset(
            [_google_function_tool("load_skill")],
            [_fake_skill(metadata={"adk_additional_tools": ["refund"]})],
            provided=[_google_function_tool("refund")],
            prefix="ops",
        )

        manifest = self._manifest(toolset, _google_function_tool("ops_refund"))

        assert [t.name for t in manifest.tools] == ["ops_refund", "ops_load_skill"]

    async def test_real_prefixed_toolset_matches_the_runtime_tool_names(self, tmp_path):
        """The manifest must name exactly what the model can call. Compares the
        recorded names against a genuine SkillToolset's own output with the
        skill activated — the one assertion that cannot drift from ADK."""
        pytest.importorskip("google.adk.tools.skill_toolset")
        from google.adk.skills import load_skill_from_dir
        from google.adk.tools.skill_toolset import SkillToolset

        def refund(order_id: str) -> str:
            """Refund an order."""
            return order_id

        toolset = SkillToolset(
            [load_skill_from_dir(_write_skill_dir(tmp_path))],
            additional_tools=[refund],
        )
        # Assigned post-construction: tool_name_prefix is public on BaseToolset
        # from 1.14, while SkillToolset forwards it only from 2.4.0.
        toolset.tool_name_prefix = "ops"

        manifest = create_google_manifest(_google_agent([toolset]))
        runtime = await toolset.get_tools_with_prefix(
            _activated_skill_context("refunder")
        )

        assert sorted(t.name for t in manifest.tools) == sorted(t.name for t in runtime)
        assert "ops_refund" in {t.name for t in manifest.tools}


# --- LangGraph tool-surface discovery (#249) --------------------------------


def _lc_tool(name: str):
    from langchain_core.tools import tool

    @tool(name)
    def _impl(text: str) -> str:
        """Stub tool."""
        return text

    return _impl


def _graph_with_bound_tools(name: str, bound_tools: list):
    """Stub graph exposing tools the way a compiled ToolNode does."""
    from types import SimpleNamespace

    tools_node = SimpleNamespace(
        bound=SimpleNamespace(tools_by_name={t.name: t for t in bound_tools})
    )
    return SimpleNamespace(name=name, nodes={"tools": tools_node})


def test_langchain_manifest_discovers_graph_bound_tools():
    refund = _lc_tool("issue_refund")
    graph = _graph_with_bound_tools(
        "support", [refund, _lc_tool("execute"), _lc_tool("write_file")]
    )

    manifest = create_langchain_manifest(graph, [refund])

    assert [t.name for t in manifest.tools] == [
        "issue_refund",
        "execute",
        "write_file",
    ]


def test_langchain_manifest_keeps_caller_tools_not_on_the_graph():
    graph = _graph_with_bound_tools("support", [_lc_tool("execute")])

    manifest = create_langchain_manifest(graph, [_lc_tool("elsewhere")])

    assert [t.name for t in manifest.tools] == ["elsewhere", "execute"]


def test_langchain_manifest_does_not_duplicate_a_shared_tool():
    refund = _lc_tool("issue_refund")
    graph = _graph_with_bound_tools("support", [refund])

    manifest = create_langchain_manifest(graph, [refund])

    assert [t.name for t in manifest.tools] == ["issue_refund"]


def test_langchain_manifest_unchanged_for_a_graph_without_a_tools_node():
    """A plain compiled graph records exactly the caller's list, as before."""
    manifest = create_langchain_manifest(
        _graph("plain"), [_lc_tool("a"), _lc_tool("b")]
    )

    assert [t.name for t in manifest.tools] == ["a", "b"]


def test_langchain_manifest_tool_order_is_deterministic():
    """Caller order first, then node order — stable across builds so the
    manifest hash does not churn on every re-register."""
    caller = [_lc_tool("z_caller"), _lc_tool("a_caller")]
    bound = [_lc_tool("m_bound"), _lc_tool("b_bound"), caller[0]]

    first = create_langchain_manifest(_graph_with_bound_tools("s", bound), caller)
    second = create_langchain_manifest(_graph_with_bound_tools("s", bound), caller)

    assert [t.name for t in first.tools] == [
        "z_caller",
        "a_caller",
        "m_bound",
        "b_bound",
    ]
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_langchain_manifest_discovers_tools_of_a_real_create_agent_graph():
    """The stub shape matches what langchain's create_agent actually compiles."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    graph = create_agent(
        model=GenericFakeChatModel(messages=iter([])),
        tools=[_lc_tool("issue_refund")],
        name="real",
    )

    manifest = create_langchain_manifest(graph, [])

    assert [t.name for t in manifest.tools] == ["issue_refund"]


def test_langchain_manifest_renders_a_tool_with_an_injected_tool_runtime():
    """A ToolRuntime field has no JSON schema; rendering args_schema raised."""
    from langchain.tools import ToolRuntime
    from langchain_core.tools import tool

    @tool
    def ls(path: str, runtime: ToolRuntime) -> str:
        """List files."""
        return path

    manifest = create_langchain_manifest(_graph_with_bound_tools("fs", [ls]), [])

    [definition] = manifest.tools
    assert list(definition.input_schema.properties) == ["path"]
    assert definition.input_schema.required == ["path"]


def test_langchain_manifest_excludes_injected_state_and_tool_call_id():
    """Injected params are not model inputs, so they must not be recorded as
    required arguments."""
    from typing import Annotated

    from langchain_core.tools import InjectedToolCallId, tool
    from langgraph.prebuilt import InjectedState

    @tool
    def refund(
        order_id: str,
        state: Annotated[dict, InjectedState],
        call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        """Refund an order."""
        return order_id

    manifest = create_langchain_manifest(_graph("plain"), [refund])

    [definition] = manifest.tools
    assert list(definition.input_schema.properties) == ["order_id"]
    assert definition.input_schema.required == ["order_id"]


# --- deepagents skill discovery ---------------------------------------------

_SKILLS_ROOT = "/skills/project"
_DEEPAGENTS_SKILLS_MODULE = "deepagents.middleware.skills"
_LANGCHAIN_MANIFEST_LOGGER = "hexgate.manifest.langchain"


class _FakeDownload:
    def __init__(self, path: str, content: bytes | None, error: str | None = None):
        self.path = path
        self.content = content
        self.error = error


class _FakeSkillsBackend:
    """Serves SKILL.md bodies; ``listing`` stands in for deepagents' parser."""

    def __init__(self, listing: dict[str, list[dict]], bodies: dict[str, bytes]):
        self.listing = listing
        self.bodies = bodies
        self.download_calls: list[list[str]] = []

    def download_files(self, paths: list[str]) -> list[_FakeDownload]:
        self.download_calls.append(list(paths))
        return [
            _FakeDownload(p, self.bodies[p])
            if p in self.bodies
            else _FakeDownload(p, None, "file_not_found")
            for p in paths
        ]


class _FakeSkillsMiddleware:
    def __init__(self, backend, sources: list[str], labels: list[str] | None = None):
        self._backend = backend
        self.sources = sources
        self.source_labels = labels if labels is not None else list(sources)


def _skill_meta(name: str, root: str = _SKILLS_ROOT, **extra) -> dict:
    return {
        "path": f"{root}/{name}/SKILL.md",
        "name": name,
        "description": f"{name} skill",
        "allowed_tools": [],
        **extra,
    }


@pytest.fixture
def fake_deepagents(monkeypatch):
    """Install a stand-in ``deepagents.middleware.skills`` backed by the fake backend."""
    import sys
    import types

    module = types.ModuleType(_DEEPAGENTS_SKILLS_MODULE)
    module._list_skills = lambda backend, source: backend.listing.get(source, [])
    monkeypatch.setitem(sys.modules, _DEEPAGENTS_SKILLS_MODULE, module)
    return module


def _skills_manifest(middleware, tools: list | None = None):
    return create_langchain_manifest(
        _graph_with_bound_tools("deep", tools or []), [], skills_middleware=middleware
    )


def test_langchain_manifest_records_deepagents_skills(fake_deepagents):
    meta = _skill_meta("refunder")
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [meta]}, {meta["path"]: b"# body"})

    manifest = _skills_manifest(_FakeSkillsMiddleware(backend, [_SKILLS_ROOT]))

    [skill] = manifest.skills
    assert (skill.name, skill.description) == ("refunder", "refunder skill")
    assert skill.additional_tools == []


def test_deepagents_skill_resources_is_none(fake_deepagents):
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [_skill_meta("refunder")]}, {})

    manifest = _skills_manifest(_FakeSkillsMiddleware(backend, [_SKILLS_ROOT]))

    assert manifest.skills[0].resources is None


def test_deepagents_allowed_tools_is_not_resplit(fake_deepagents):
    meta = _skill_meta("refunder", allowed_tools=["read_file", "issue_refund"])
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [meta]}, {})

    manifest = _skills_manifest(_FakeSkillsMiddleware(backend, [_SKILLS_ROOT]))

    assert manifest.skills[0].allowed_tools == ["read_file", "issue_refund"]


def test_deepagents_skill_content_hash_is_the_body_digest(fake_deepagents):
    a, b = _skill_meta("alpha"), _skill_meta("beta")
    bodies = {a["path"]: b"alpha body", b["path"]: b"beta body"}
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [a, b]}, bodies)

    manifest = _skills_manifest(_FakeSkillsMiddleware(backend, [_SKILLS_ROOT]))

    assert {s.name: s.content_hash for s in manifest.skills} == {
        "alpha": hashlib.sha256(b"alpha body").hexdigest(),
        "beta": hashlib.sha256(b"beta body").hexdigest(),
    }
    assert backend.download_calls == [[a["path"], b["path"]]]


def test_content_hash_omitted_when_the_body_cannot_be_read(fake_deepagents):
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [_skill_meta("refunder")]}, {})

    manifest = _skills_manifest(_FakeSkillsMiddleware(backend, [_SKILLS_ROOT]))

    assert manifest.skills[0].content_hash is None


def test_later_source_wins_on_a_name_collision(fake_deepagents):
    base, project = "/skills/base", "/skills/project"
    listing = {
        base: [_skill_meta("refunder", root=base, description="base")],
        project: [_skill_meta("refunder", root=project, description="project")],
    }
    backend = _FakeSkillsBackend(listing, {})

    manifest = _skills_manifest(
        _FakeSkillsMiddleware(backend, [base, project], ["Base", "Project"])
    )

    [skill] = manifest.skills
    assert (skill.description, skill.source) == ("project", "Project")


def test_source_label_is_recorded_from_source_labels(fake_deepagents):
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [_skill_meta("refunder")]}, {})

    manifest = _skills_manifest(
        _FakeSkillsMiddleware(backend, [_SKILLS_ROOT], ["User Claude"])
    )

    assert manifest.skills[0].source == "User Claude"


def test_source_label_falls_back_to_the_path_without_aligned_labels(fake_deepagents):
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [_skill_meta("refunder")]}, {})

    manifest = _skills_manifest(_FakeSkillsMiddleware(backend, [_SKILLS_ROOT], []))

    assert manifest.skills[0].source == _SKILLS_ROOT


def test_no_middleware_leaves_skills_none():
    manifest = create_langchain_manifest(_graph_with_bound_tools("deep", []), [])

    assert manifest.skills is None
    assert "skills" not in manifest.model_dump(exclude_none=True)


def test_middleware_without_skills_leaves_skills_none(fake_deepagents):
    backend = _FakeSkillsBackend({}, {})

    manifest = _skills_manifest(_FakeSkillsMiddleware(backend, [_SKILLS_ROOT]))

    assert manifest.skills is None


def test_missing_backend_attr_degrades_to_no_skills(fake_deepagents, caplog):
    from types import SimpleNamespace

    with caplog.at_level(logging.WARNING, logger=_LANGCHAIN_MANIFEST_LOGGER):
        manifest = _skills_manifest(SimpleNamespace(sources=[_SKILLS_ROOT]))

    assert manifest.skills is None
    assert "no skills recorded" in caplog.text


def test_deepagents_absent_degrades_to_no_skills(monkeypatch, caplog):
    import sys

    monkeypatch.setitem(sys.modules, _DEEPAGENTS_SKILLS_MODULE, None)
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [_skill_meta("refunder")]}, {})

    with caplog.at_level(logging.WARNING, logger=_LANGCHAIN_MANIFEST_LOGGER):
        manifest = _skills_manifest(_FakeSkillsMiddleware(backend, [_SKILLS_ROOT]))

    assert manifest.skills is None
    assert "skill listing unavailable" in caplog.text


def test_skills_do_not_disturb_the_discovered_tool_list(fake_deepagents):
    bound = [_lc_tool("read_file"), _lc_tool("execute")]
    backend = _FakeSkillsBackend({_SKILLS_ROOT: [_skill_meta("refunder")]}, {})

    with_skills = _skills_manifest(
        _FakeSkillsMiddleware(backend, [_SKILLS_ROOT]), bound
    )
    without = create_langchain_manifest(_graph_with_bound_tools("deep", bound), [])

    assert with_skills.tools == without.tools
