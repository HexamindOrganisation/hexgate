from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent
REPO = DEMO.parent
RUN = DEMO / ".run"          # logs, pids, build stamps (gitignored)
TOOLS = DEMO / ".tools"      # node, go, uv, opa installed by demo/run (gitignored)
APPS = DEMO / "apps"
REFERENCE = DEMO / "reference"
ENV_FILE = DEMO / ".env"     # OPENAI_API_KEY for every demo

API_PORT = 8000
MCP_PORT = 8080
COLLECTOR_PORT = 4318
API = f"http://localhost:{API_PORT}"
POSTGRES_DSN = "postgresql+asyncpg://hexgate:hexgate-dev-password@localhost:5433/hexgate"
