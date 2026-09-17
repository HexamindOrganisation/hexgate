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
