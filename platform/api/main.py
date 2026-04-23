from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Fortify API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Unversioned liveness probe."""
    return {"status": "ok", "service": "fortify-api"}


v1 = APIRouter(prefix="/v1")


@v1.get("/health")
def v1_health() -> dict[str, str]:
    """Versioned health — reachable from the frontend via Vite proxy."""
    return {"status": "ok", "service": "fortify-api", "version": "v1"}


app.include_router(v1)
