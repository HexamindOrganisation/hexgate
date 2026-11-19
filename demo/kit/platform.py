"""The local hexgate platform: Docker infra, API + dashboard, MCP server, audit pipeline.

API + dashboard  :8000  demo mode (/v1/demo-login signs in, no password)
MCP server       :8080  what Claude Code talks to
collector        :4318  receives the SDK's decisions; the enricher writes them to ClickHouse
Docker           Postgres :5433, Redpanda, ClickHouse :8124
"""

from __future__ import annotations

import hashlib
import os
import platform as _platform
import shutil
import subprocess
import sys
import tarfile
import io
import time
from pathlib import Path

from kit import procs, ui
from kit.paths import (
    API,
    API_PORT,
    COLLECTOR_PORT,
    MCP_PORT,
    POSTGRES_DSN,
    REPO,
    RUN,
    TOOLS,
)

SERVICES = ("api", "mcp", "collector", "enricher")


def tool_env() -> dict[str, str]:
    """PATH with the tools demo/run installed in demo/.tools first."""
    extra = [TOOLS / "uv", TOOLS / "node" / "bin", TOOLS / "go" / "bin"]
    path = os.pathsep.join(
        [str(p) for p in extra if p.exists()] + [os.environ.get("PATH", "")]
    )
    env = {"PATH": path}
    if (TOOLS / "go").exists():
        env["GOROOT"] = str(TOOLS / "go")
    return env


def _run(cmd: list[str], cwd: Path, label: str, env: dict | None = None) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    log = RUN / "build.log"
    with ui.spinner(label), open(log, "ab") as f:
        f.write(f"\n==== {label}: {' '.join(cmd)}\n".encode())
        f.flush()
        r = subprocess.run(
            cmd,
            cwd=cwd,
            env={**os.environ, **tool_env(), **(env or {})},
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    if r.returncode != 0:
        ui.fail(f"{label} failed. Last lines of {log}:")
        for line in log.read_text(errors="replace").splitlines()[-20:]:
            print("     " + line)
        raise SystemExit(1)


# ------------------------------------------------------------ docker


def docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def ensure_docker() -> None:
    if docker_ready():
        return
    if not shutil.which("docker"):
        ui.fail("Docker is not installed. Run ./demo/run, it installs it.")
        raise SystemExit(1)
    if sys.platform == "darwin":
        ui.step("starting Docker Desktop")
        subprocess.run(["open", "-a", "Docker"], capture_output=True)
    else:
        ui.warn(
            "Docker is installed but not running. Start it (sudo systemctl start docker)."
        )
    with ui.spinner("waiting for Docker"):
        for _ in range(180):
            if docker_ready():
                return
            time.sleep(1)
    ui.fail("Docker did not start.")
    raise SystemExit(1)


# ------------------------------------------------------------ builds


def _tree_stamp(rel: str) -> str:
    """Changes whenever the committed or working-tree sources under `rel` change."""
    h = hashlib.sha256()
    for args in (
        ["ls-files", "-s", rel],
        ["diff", "HEAD", "--", rel],
        ["ls-files", "-o", "--exclude-standard", rel],
    ):
        h.update(subprocess.run(["git", *args], cwd=REPO, capture_output=True).stdout)
    # Untracked files by content too, so editing one before `git add` rebuilds.
    untracked = subprocess.run(["git", "ls-files", "-o", "--exclude-standard", "-z", rel],
                               cwd=REPO, capture_output=True).stdout
    if untracked:
        h.update(subprocess.run(["git", "hash-object", "--stdin-paths"], cwd=REPO, capture_output=True,
                                input=untracked.replace(b"\0", b"\n")).stdout)
    return h.hexdigest()[:16]


def _stamp_ok(name: str, stamp: str, artifact: Path) -> bool:
    f = RUN / f"{name}.stamp"
    return artifact.exists() and f.exists() and f.read_text() == stamp


def _set_stamp(name: str, stamp: str) -> None:
    (RUN / f"{name}.stamp").write_text(stamp)


def pnpm() -> list[str]:
    if shutil.which("pnpm", path=tool_env()["PATH"]):
        return ["pnpm"]
    if shutil.which("corepack", path=tool_env()["PATH"]):
        return ["corepack", "pnpm"]
    raise SystemExit("neither pnpm nor corepack found: install pnpm (npm i -g pnpm) and run again")


def build_ui(src: Path, out: Path, label: str) -> None:
    """Build a demo app's web UI (a Vite project in `src`, writing to `out`)
    once per source change."""
    stamp = _tree_stamp(str(src.relative_to(REPO)))
    name = f"ui-{src.parent.name}"
    if _stamp_ok(name, stamp, out / "index.html"):
        return
    cmd = pnpm()
    _run([*cmd, "install", "--frozen-lockfile"], src, f"installing {label} UI packages")
    _run([*cmd, "build"], src, f"building the {label} UI")
    _set_stamp(name, stamp)


def dashboard_dist() -> Path:
    """Build the dashboard once per source change. Falls back to the last
    commit when the working tree doesn't build (someone's half-done edit)."""
    src = REPO / "platform" / "dashboard"
    stamp = _tree_stamp("platform/dashboard")
    # Each build has its own stamp: a fallback must not vouch for an old src/dist.
    for name, candidate in (("dashboard", src / "dist"), ("dashboard-head", RUN / "dashboard-head" / "dist")):
        if _stamp_ok(name, stamp, candidate / "index.html"):
            return candidate
    cmd = pnpm()
    _run(
        [*cmd, "install", "--frozen-lockfile"],
        src,
        "installing dashboard packages (first run only)",
    )
    try:
        _run([*cmd, "build"], src, "building the dashboard")
        _set_stamp("dashboard", stamp)
        return src / "dist"
    except SystemExit:
        ui.warn(
            "the working tree's dashboard doesn't build; building the last commit instead"
        )
    head = RUN / "dashboard-head"
    shutil.rmtree(head, ignore_errors=True)
    head.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", "HEAD", "platform/dashboard"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as t:
        t.extractall(head, filter="data")
    built = head / "platform" / "dashboard"
    (built / "node_modules").symlink_to(src / "node_modules")
    _run([*cmd, "build"], built, "building the dashboard (last commit)")
    final = head / "dist"
    shutil.move(str(built / "dist"), final)
    _set_stamp("dashboard-head", stamp)
    return final


def collector_binary() -> Path:
    src = REPO / "platform" / "collector"
    binary = src / "hexgate-collector"
    stamp = _tree_stamp("platform/collector")
    if not _stamp_ok("collector", stamp, binary):
        _run(
            ["go", "build", "-o", "hexgate-collector", "."],
            src,
            "building the collector (first run only)",
        )
        _set_stamp("collector", stamp)
    return binary


def python_envs() -> None:
    for rel in ("platform/api", "platform/mcp"):
        _run(
            ["uv", "sync", "--frozen", "--python", "3.13"],
            REPO / rel,
            f"Python packages for {rel}",
        )


# ------------------------------------------------------------ up / down


def is_up() -> bool:
    return procs.http_ok(f"{API}/health") and procs.running("api") is not None


def _check_ports() -> None:
    for name, port in (
        ("api", API_PORT),
        ("mcp", MCP_PORT),
        ("collector", COLLECTOR_PORT),
    ):
        if procs.running(name) or not procs.port_busy(port):
            continue
        owner = procs.port_owner(port)
        ui.warn(f"port {port} is taken by something else: {owner or 'unknown process'}")
        pid = owner.split(" ")[0] if owner else ""
        # Default no: without a terminal (a script, an agent) nothing is killed.
        if pid.isdigit() and ui.ask(
            f"stop it (pid {pid}) so the demo can use port {port}?", default=False
        ):
            procs.kill_tree(int(pid))
            time.sleep(1)
        else:
            raise SystemExit(f"port {port} is busy; free it and run again.")


def up() -> None:
    if is_up() and all(procs.running(s) for s in SERVICES):
        ui.ok("platform already running")
        return
    ui.step("platform")
    ensure_docker()
    _check_ports()
    python_envs()
    dist = dashboard_dist()
    collector = collector_binary()
    env = tool_env()

    _run(
        ["make", "-s", "postgres-init", "redpanda-topics", "clickhouse-migrate"],
        REPO,
        "starting Postgres, Redpanda and ClickHouse",
    )
    ui.ok("Docker infra")

    if not procs.running("api"):
        procs.start(
            "api",
            [
                str(REPO / "platform/api/.venv/bin/uvicorn"),
                "hexgate_api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(API_PORT),
            ],
            REPO / "platform/api",
            {
                **env,
                "DATABASE_URL": POSTGRES_DSN,
                "HEXGATE_DEMO": "1",
                "HEXGATE_COOKIE_SECURE": "0",
                "HEXGATE_SEED_AGENTS": "skip",
                "HEXGATE_DASHBOARD_URL": API,
                "HEXGATE_DASHBOARD_DIST": str(dist),
            },
        )
    if not procs.wait_http("api", f"{API}/health", 120):
        raise SystemExit(1)
    ui.ok(f"API + dashboard   {API}")

    if not procs.running("mcp"):
        procs.start(
            "mcp",
            [str(REPO / "platform/mcp/.venv/bin/python"), "-m", "hexgate_mcp"],
            REPO / "platform/mcp",
            env,
        )
    if not procs.wait_http("mcp", f"http://localhost:{MCP_PORT}/health", 120):
        raise SystemExit(1)
    ui.ok(f"MCP server        http://localhost:{MCP_PORT}/mcp")

    if not procs.running("collector"):
        procs.start(
            "collector",
            [str(collector), "--config=config.yaml"],
            REPO / "platform/collector",
            env,
        )
    if not procs.running("enricher"):
        procs.start(
            "enricher",
            [
                str(REPO / "platform/api/.venv/bin/python"),
                "-m",
                "hexgate_api.jobs.enricher",
            ],
            REPO / "platform/api",
            {**env, "DATABASE_URL": POSTGRES_DSN},
        )
    if procs.wait_http("collector", f"http://localhost:{COLLECTOR_PORT}", 60):
        ui.ok(f"audit pipeline    collector :{COLLECTOR_PORT} + enricher")


def down(docker: bool = True) -> None:
    ui.step("stopping")
    for name in procs.names():
        if procs.stop(name):
            ui.ok(f"stopped {name}")
    if docker and docker_ready():
        _run(
            ["make", "-s", "postgres-stop", "redpanda-stop", "clickhouse-down"],
            REPO,
            "stopping the Docker containers (data is kept)",
        )
        ui.ok("Docker containers stopped")


def machine() -> str:
    return f"{sys.platform}/{_platform.machine()}"
