from fortify.cli.register.manifest import create_manifest
from fortify.cli.register.models import AgentManifest, AgentFramework, InputProperty, InputSchema, ToolDefinition
from fortify.cli.register.openai import create_openai_manifest
from fortify.cli.register.google import create_google_manifest
from fortify.cli.register.langchain import create_langchain_manifest
from fortify.cli.register.pydantic_ai import create_pydantic_ai_manifest

def test_agent_manifest_schema():
    """Test the schema of the agent manifest."""
    manifest = AgentManifest(
        name="test-agent",
        description="A test agent",
        framework=AgentFramework.FORTIFY,
        tools=[],
    )
    assert manifest.name == "test-agent"
    assert manifest.description == "A test agent"
    assert manifest.framework == AgentFramework.FORTIFY
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
        tools=[ToolDefinition(
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
        )],
    )
    manifest = create_openai_manifest(agent)
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
        tools=[ToolDefinition(
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
        )],
    )
    manifest = create_google_manifest(agent)
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest

    manifest = create_manifest(agent, description="A test agent")
    assert isinstance(manifest, AgentManifest)
    assert manifest == expected_manifest

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
        tools=[ToolDefinition(
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
        )],
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
        tools=[ToolDefinition(
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
        )],
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