import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    # The async tests run on anyio's pytest plugin (installed with Pydantic
    # AI), on asyncio only, as the app does under uvicorn. Session scope lets
    # the fake MCP server live for a whole module.
    return "asyncio"
