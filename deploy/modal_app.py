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
    .apt_install("nodejs", "npm", "git")
    .run_commands("npm install -g pnpm")
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


@app.function(
    image=image,
    timeout=60 * 60,            # max session length
    scaledown_window=600,       # die 10 min after the last request → world evaporates
    max_containers=50,          # cap concurrent live sessions (cost guardrail)
    # No LLM secret needed: the visitor brings their own OpenAI key (entered in
    # the notebook), so no provider key ever lives in the image or the platform.
)
@modal.concurrent(max_inputs=1)  # one visitor per container — isolation
@modal.web_server(MARIMO_PORT, startup_timeout=180)
def demo():
    import os
    import sys

    os.chdir(REMOTE_ROOT)
    sys.path.insert(0, f"{REMOTE_ROOT}/deploy")
    os.environ.setdefault("HEXGATE_MARIMO_PORT", str(MARIMO_PORT))
    os.environ.setdefault("HEXGATE_API_PORT", str(API_PORT))

    import boot

    # Open a public tunnel to the dashboard port, hand its URL to boot (which
    # forwards it to the notebook), then start everything. The tunnel stays
    # open for the life of the container because we never leave the `with`.
    with modal.forward(API_PORT) as dash_tunnel:
        print(f"[modal] dashboard tunnel: {dash_tunnel.url}", flush=True)
        boot.run(dash_url=dash_tunnel.url)
