import logging

import pytest

from hexgate.manifest import create_manifest
from hexgate.manifest.google import create_google_manifest
from hexgate.manifest.langchain import create_langchain_manifest
from hexgate.manifest.models import (
    AgentFramework,
    AgentManifest,
    InputProperty,
    InputSchema,
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
