"""Modal wrapper — one disposable Hexgate demo world per visitor session.

The container runs the whole stack (API + dashboard + hexgate serve + marimo)
via boot.py. marimo (:2718) is the stable entry URL exposed by
``@modal.web_server``; the dashboard (:8000) is exposed at runtime with
``modal.forward`` and its public URL is handed to the notebook so the
"Open the live playground" link points at the real tunnel.

Deploy:   modal deploy deploy/modal_app.py
Dev:      modal serve  deploy/modal_app.py

Image build note: the dashboard is built (`pnpm build`) during image build so
the API can serve `dist/` same-origin. That pulls Node into the image. If you'd
rather keep the image lean, build dist/ locally and swap the Node steps for
`.add_local_dir("platform/dashboard/dist", "/app/platform/dashboard/dist")`.
"""

import modal

ASIANF_LOCAL = "."  # run `modal deploy` from the asianf/ directory
REMOTE_ROOT = "/app"

API_VENV = f"{REMOTE_ROOT}/platform/api/.venv"

image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git", "curl", "ca-certificates")
    # debian's apt ships Node 18, but current pnpm needs Node >=22 and the repo
    # builds on Node 20 + pnpm 9 (see .github/workflows/release.yml). Install
    # Node 20 from NodeSource and pin pnpm 9 to match the committed lockfile.
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
        "apt-get install -y nodejs",
        "npm install -g pnpm@9",
    )
    .pip_install("uv")
    .add_local_dir(ASIANF_LOCAL, REMOTE_ROOT, copy=True, ignore=[
        "**/node_modules", "**/.venv", "**/__pycache__", "**/*.db", "**/dist",
    ])
    .run_commands(
        # The platform API is an application project (no build backend), so it
        # installs via `uv sync` from its own uv.lock — reproducible, and its
        # lockfile already pulls in `hexgate` (path source → /app), so this one
        # venv provides BOTH the API and the `hexgate` CLI. Add marimo into it.
        f"cd {REMOTE_ROOT}/platform/api && uv sync --frozen",
        f"uv pip install --python {API_VENV} marimo",
        # Build the dashboard so the API can serve it same-origin from dist/.
        f"cd {REMOTE_ROOT}/platform/dashboard && pnpm install --frozen-lockfile && pnpm build",
    )
    # Put the API venv on PATH so boot.py's bare `uvicorn` / `hexgate` /
    # `marimo` / `python` all resolve to the same interpreter + site-packages.
    .env({"PATH": f"{API_VENV}/bin:/usr/local/bin:/usr/bin:/bin"})
)

app = modal.App("hexgate-demo")

MARIMO_PORT = 2718
API_PORT = 8000

# Holds the dashboard forward tunnel so it survives the function returning
# (entered, never exited — lives for the container's life).
_dash_tunnel = None


@app.function(
    image=image,
    # Two heavy Python processes run per container — the API (imports the
    # platform + hexgate) and the marimo kernel (imports hexgate for the agent
    # + in-kernel serve loop). The langgraph/litellm/google-adk import alone is
    # ~1-2 GiB each, so an under-provisioned container OOM-kills on startup and
    # never goes ready ("cold-starting forever"). Give it real headroom; dial
    # down later from the dashboard memory metric once we know true usage.
    cpu=2,
    memory=8192,                # 8 GiB
    timeout=60 * 60,            # max session length
    scaledown_window=600,       # die 10 min after the last request → world evaporates
    max_containers=50,          # cap concurrent live sessions (cost guardrail)
    # No LLM secret needed: the visitor brings their own OpenAI key (entered in
    # the notebook), so no provider key ever lives in the image or the platform.
)
@modal.web_server(MARIMO_PORT, startup_timeout=300)
def demo():
    import os
    import subprocess
    import sys

    os.chdir(REMOTE_ROOT)
    sys.path.insert(0, f"{REMOTE_ROOT}/deploy")
    os.environ.setdefault("HEXGATE_MARIMO_PORT", str(MARIMO_PORT))
    os.environ.setdefault("HEXGATE_API_PORT", str(API_PORT))

    import boot

    # @modal.web_server contract: the function should LAUNCH the server and
    # RETURN — Modal then polls the port and flips the container to "live". A
    # blocking function (foreground server OR a block-loop) leaves it stuck
    # "cold-starting" even though the port answers (confirmed: curl :2718 → 200).
    #
    # So: open the dashboard tunnel and keep it alive past return by stashing it
    # in a module global (never call __exit__); bring up the API + mint the key;
    # spawn marimo in the BACKGROUND; then return so Modal can mark us ready.
    global _dash_tunnel
    _dash_tunnel = modal.forward(API_PORT)
    dash = _dash_tunnel.__enter__()
    print(f"[modal] dashboard tunnel: {dash.url}", flush=True)
    env = boot.start_services(dash_url=dash.url)
    subprocess.Popen(boot.marimo_argv(), cwd=str(boot.ASIANF), env=env)
    print("[modal] marimo spawned; returning so web_server can go ready", flush=True)
