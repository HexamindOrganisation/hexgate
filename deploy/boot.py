"""Boot one self-contained demo world inside a single container.

Starts, in order, in one process tree:

  1. Platform API (uvicorn ``main:app``) on :8000 — its lifespan creates a
     fresh SQLite, seeds one org/user/project/agents, and (with HEXGATE_DEMO=1)
     serves the built dashboard same-origin + exposes /v1/demo-login.
  2. A minted HEXGATE_KEY for the seeded project (see provision.py).
  3. ``hexgate serve <AGENT_SPEC>`` — dials out to the local API's /v1/serve
     and runs the agent, streaming events back through the relay.
  4. marimo (the notebook UI the visitor lands on) on :2718.

Then it blocks, holding everything up until a child dies or it's signalled.
Container scaledown takes the whole world (SQLite, org, project) with it —
nothing to garbage-collect in a shared DB.

Runs locally for testing (``python deploy/boot.py``) and is the entrypoint the
Modal wrapper calls. ``dash_url`` (the public dashboard URL) is written to
``/tmp/hexgate_dash_url`` for the notebook to read and render as a link.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ASIANF = Path(__file__).resolve().parent.parent          # .../asianf
API_DIR = ASIANF / "platform" / "api"
DEPLOY_DIR = Path(__file__).resolve().parent
NOTEBOOK = DEPLOY_DIR / "demo_notebook.py"

API_PORT = int(os.environ.get("HEXGATE_API_PORT", "8000"))
MARIMO_PORT = int(os.environ.get("HEXGATE_MARIMO_PORT", "2718"))
DASH_URL_FILE = Path(os.environ.get("HEXGATE_DASH_URL_FILE", "/tmp/hexgate_dash_url"))
SERVE_KEY_FILE = Path(os.environ.get("HEXGATE_SERVE_KEY_FILE", "/tmp/hexgate_serve_key"))

_procs: list[subprocess.Popen] = []


def _spawn(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    print(f"[boot] starting: {' '.join(cmd)}  (cwd={cwd})", flush=True)
    p = subprocess.Popen(cmd, cwd=str(cwd), env=env)
    _procs.append(p)
    return p


def _wait_healthy(url: str, timeout: float = 90.0) -> None:
    """Poll a public, no-auth endpoint until the API answers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    print(f"[boot] API healthy at {url}", flush=True)
                    return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"API did not become healthy at {url} within {timeout}s")


def run(dash_url: str | None = None) -> None:
    # --- env shared by all children -------------------------------------
    env = dict(os.environ)
    env.setdefault("HEXGATE_DEMO", "1")           # API serves dashboard + demo-login
    env.setdefault("HEXGATE_COOKIE_SECURE", "1")  # cookie rides the https tunnel
    api_base = f"http://127.0.0.1:{API_PORT}"
    env["HEXGATE_API_URL"] = api_base
    env["HEXGATE_ROOT"] = str(ASIANF)  # so the notebook can find examples/*.py
    # The API package is imported by module name; make `main`, `examples.*` importable.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(API_DIR), str(ASIANF), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)

    # Publish the dashboard URL for the notebook to read. Falls back to the
    # local API origin when not running behind a tunnel (local testing).
    DASH_URL_FILE.write_text((dash_url or api_base).strip())

    # --- 1. Platform API -------------------------------------------------
    _spawn(
        ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(API_PORT)],
        cwd=API_DIR,
        env=env,
    )
    _wait_healthy(f"{api_base}/v1/.well-known/keys")

    # --- 2. Mint the serve token (shares the now-seeded SQLite + keystore) ---
    # provision.py lives beside this file; its internal imports (db/main/services)
    # need the API dir on sys.path.
    sys.path.insert(0, str(DEPLOY_DIR))
    sys.path.insert(0, str(API_DIR))
    from provision import provision_serve_token  # noqa: E402  (path set above)

    hexgate_key = provision_serve_token()
    SERVE_KEY_FILE.write_text(hexgate_key)
    print("[boot] minted HEXGATE_KEY for seeded project", flush=True)
    # The agent's LLM key is BYOK — entered in the notebook and set into the
    # process env by serve_manager. boot doesn't touch it.

    # --- 3. (serve runs in the notebook kernel) --------------------------
    # The notebook's serve_manager runs the `hexgate serve` loop in-kernel,
    # bound to the live `agent` object, and restarts it on each "Apply" — so
    # visitor edits flow into the dashboard. boot only hands it the platform
    # key (via SERVE_KEY_FILE) + HEXGATE_API_URL (via env).

    # --- 4. marimo notebook (the landing UI) -----------------------------
    # `edit` = interactive (read the agent def, run cells, install). In an
    # isolated throwaway container there's nothing to protect, so disable the
    # access token. Swap to `marimo run` for a locked-down app-mode demo.
    _spawn(
        [
            "marimo", "edit", str(NOTEBOOK),
            "--headless", "--host", "0.0.0.0", "-p", str(MARIMO_PORT),
            "--no-token",
        ],
        cwd=ASIANF,
        env=env,
    )

    _block_until_exit()


def _block_until_exit() -> None:
    def _shutdown(*_a):
        for p in _procs:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    # If any child dies, tear the whole world down — a half-up demo is worse
    # than a clean restart.
    while True:
        for p in _procs:
            if p.poll() is not None:
                print(f"[boot] child pid={p.pid} exited ({p.returncode}); shutting down", flush=True)
                _shutdown()
        time.sleep(1.0)


if __name__ == "__main__":
    run()
