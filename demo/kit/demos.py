"""One demo = a folder in demo/apps/ with a demo.toml. Prepare, reset, secure."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from kit import platform, procs, ui
from kit.api import Api, forget_agent
from kit.paths import API, APPS, DEMO, ENV_FILE, REFERENCE, REPO


@dataclass
class Demo:
    key: str
    title: str
    project: str
    agent: str
    register: str
    agent_file: str
    reference: str
    package: str  # the app's Python package: shipyard, claimdesk
    app_command: list[str]
    app_port: int
    app_url: str
    prompts: dict[str, str]
    src: Path  # demo/apps/<demo>, the app as committed in the hexgate repo
    workspace: str = ""  # where it runs and gets filmed, e.g. ~/shipyard
    ui: str = ""  # the app's web UI source (a Vite project), relative to src
    ui_build: str = ""  # where that UI builds to, relative to src; the app serves it
    film: dict | None = None  # [film] in demo.toml: how ./demo/run film films it

    @property
    def dir(self) -> Path:
        """The app folder people see: its own git repo outside hexgate when
        `workspace` is set (so Claude sees only the app, and a diff viewer shows
        exactly what Claude changed), else the folder in the repo."""
        return Path(self.workspace).expanduser() if self.workspace else self.src

    @property
    def shown_dir(self) -> str:
        home = str(Path.home())
        d = str(self.dir)
        return "~" + d[len(home):] if d.startswith(home + "/") else d

    @property
    def ref(self) -> Path:
        return REFERENCE / self.reference

    @property
    def proc(self) -> str:
        return f"app-{self.key}"


def all_demos() -> list[Demo]:
    out = []
    for toml in sorted(APPS.glob("*/demo.toml")):
        d = tomllib.loads(toml.read_text())
        out.append(Demo(src=toml.parent, prompts=d.pop("prompts", {}), **d))
    return out


def get(key: str) -> Demo:
    for d in all_demos():
        if d.key == key:
            return d
    raise SystemExit(
        f"no demo called {key!r}; demos: {', '.join(d.key for d in all_demos())}"
    )


# ------------------------------------------------------------ env files


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def openai_key_set() -> bool:
    """Only whether the key is there; its value is never printed."""
    return bool(
        os.environ.get("OPENAI_API_KEY") or read_env(ENV_FILE).get("OPENAI_API_KEY")
    )


def _write_app_env(demo: Demo, project_id: str, key: str) -> None:
    openai = os.environ.get("OPENAI_API_KEY") or read_env(ENV_FILE).get("OPENAI_API_KEY", "")
    (demo.dir / ".env").write_text(
        "# Written by the demo kit (hexgate project key + the OpenAI key from demo/.env).\n"
        f"OPENAI_API_KEY={openai}\n"
        f"HEXGATE_API_KEY={key}\n"
        f"HEXGATE_API_URL={API}\n"
        "HEXGATE_OTLP_ENDPOINT=http://localhost:4318/v1/traces\n"
        f"HEXGATE_PROJECT_ID={project_id}\n"
        "OPENAI_AGENTS_DISABLE_TRACING=1\n"
    )


def ensure_key(demo: Demo, api: Api) -> str:
    project_id = api.ensure_project(demo.project)
    env = read_env(demo.dir / ".env")
    if env.get("HEXGATE_PROJECT_ID") == project_id and env.get("HEXGATE_API_KEY"):
        # Still valid? The collector checks the key against Postgres.
        req = urllib.request.Request(
            "http://localhost:4318/v1/traces",
            data=b"{}",
            method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {env['HEXGATE_API_KEY']}",
            },
        )
        valid = True
        try:
            urllib.request.urlopen(req, timeout=3)
        except Exception as exc:  # noqa: BLE001
            valid = getattr(exc, "code", None) not in (401, 403)
        if valid:
            _write_app_env(demo, project_id, env["HEXGATE_API_KEY"])
            return project_id
    _write_app_env(demo, project_id, api.mint_key(project_id, f"{demo.key}-demo"))
    ui.ok(f"new hexgate API key for project “{demo.project}” in {demo.shown_dir}/.env")
    return project_id


# ------------------------------------------------------------ workspace

_GITIGNORE = ".env\n.venv/\n__pycache__/\nuv.lock\n.claude/settings.local.json\n"
_MCP_JSON = '{\n  "mcpServers": {\n    "hexgate": { "type": "http", "url": "http://localhost:8080/mcp" }\n  }\n}\n'
_MARK = "hexgate-demo"  # file inside .git/: this folder belongs to the kit


def _git(demo: Demo, *args: str) -> subprocess.CompletedProcess:
    who = {"GIT_AUTHOR_NAME": "Acme Platform Team", "GIT_AUTHOR_EMAIL": "platform@acme.example"}
    who |= {"GIT_COMMITTER_NAME": who["GIT_AUTHOR_NAME"], "GIT_COMMITTER_EMAIL": who["GIT_AUTHOR_EMAIL"]}
    return subprocess.run(["git", *args], cwd=demo.dir, env={**os.environ, **who},
                          capture_output=True, text=True, check=True)


def sync_workspace(demo: Demo) -> None:
    """Make demo.dir a clean copy of the app at the start of scene 1.

    The workspace is its own git repo: a reset is `git reset --hard` + `git
    clean`, which also removes anything a take added. .env and .venv are
    ignored, so they survive."""
    if demo.ui:
        platform.build_ui(demo.src / demo.ui, demo.src / demo.ui_build, demo.key)
    if not demo.workspace:
        return
    d = demo.dir
    fresh = not (d / ".git").exists()
    if fresh and d.exists() and any(d.iterdir()):
        raise SystemExit(f"{demo.shown_dir} exists and isn't the kit's; move it away or change `workspace` in demo.toml")
    if not fresh and not (d / ".git" / _MARK).exists():
        raise SystemExit(f"{demo.shown_dir} is a git repo the kit didn't create; not touching it")
    d.mkdir(parents=True, exist_ok=True)
    if fresh:
        _git(demo, "init", "-q", "-b", "main")
        (d / ".git" / _MARK).write_text("created by hexgate's demo kit (demo/run)\n")
    else:
        _git(demo, "reset", "-q", "--hard")
        _git(demo, "clean", "-fdq")
        # Start from no tracked files, so a file the source dropped goes too.
        _git(demo, "rm", "-rqf", "--ignore-unmatch", ".")
    # The app as committed in the hexgate repo; the skill symlink is copied as files.
    shutil.copytree(demo.src, d, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".venv", "__pycache__", ".env", "demo.toml",
                                                  "node_modules", *([demo.ui] if demo.ui else [])))
    py = d / "pyproject.toml"
    py.write_text(py.read_text().replace('path = "../../.."', f'path = "{REPO}"'))
    (d / ".gitignore").write_text(_GITIGNORE)
    (d / ".mcp.json").write_text(_MCP_JSON)
    _git(demo, "add", "-A")
    if _git(demo, "status", "--porcelain").stdout.strip():
        name = demo.package.capitalize()
        _git(demo, "commit", "-q", "-m", f"{name} copilot" if fresh else f"Update {name}")
    ui.ok(f"app folder {demo.shown_dir} (its own git repo, clean)")


# ------------------------------------------------------------ app


def app_env(demo: Demo, scripted: bool) -> dict[str, str]:
    env = {f"{demo.package.upper()}_MODEL": "scripted"} if scripted else {}
    return {**platform.tool_env(), **env}


def start_app(demo: Demo, scripted: bool) -> None:
    procs.stop(demo.proc)
    if procs.port_busy(demo.app_port):
        owner = procs.port_owner(demo.app_port)
        raise SystemExit(f"port {demo.app_port} is taken by something else ({owner or 'unknown process'}); "
                         f"free it and run again")
    platform._run(
        ["uv", "sync", "--python", "3.13"],
        demo.dir,
        f"Python packages for the {demo.key} app",
    )
    cmd = [str(demo.dir / ".venv/bin" / demo.app_command[0]), *demo.app_command[1:]]
    procs.start(
        demo.proc,
        cmd,
        demo.dir,
        {**app_env(demo, scripted), f"{demo.package.upper()}_PORT": str(demo.app_port)},
    )
    if not procs.wait_http(demo.proc, f"{demo.app_url}/login", 60):
        raise SystemExit(1)
    ui.ok(
        f"{demo.title.split(':')[0]} app     {demo.app_url}"
        + ("   (scripted model)" if scripted else "")
    )


def app_post(demo: Demo, path: str) -> bool:
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{demo.app_url}{path}", data=b"{}", method="POST"),
            timeout=5,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------ commands


def prepare(demo: Demo, open_browser: bool = True) -> None:
    platform.up()
    ui.step(f"demo: {demo.title}")
    sync_workspace(demo)
    api = Api()
    ensure_key(demo, api)

    scripted = False
    if not openai_key_set():
        ui.warn("no OPENAI_API_KEY in demo/.env")
        ui.info(
            "put it there for the real model (gpt-4o-mini), or rehearse with a scripted one"
        )
        scripted = ui.ask("start with the scripted model for now?", True)
        if not scripted:
            raise SystemExit("add OPENAI_API_KEY=... to demo/.env and run again.")
    start_app(demo, scripted)

    if open_browser:
        webbrowser.open(demo.app_url)
        webbrowser.open(f"{API}/v1/demo-login")
    _next_steps(demo)


def _next_steps(demo: Demo) -> None:
    print()
    print(ui.bold("Ready to film."))
    print(
        f"  app        {demo.app_url}   (accounts in {demo.shown_dir}/users.yaml)"
    )
    print(f"  dashboard  {API}/v1/demo-login   (signs you in, no password)")
    print(f"             then pick the project “{demo.project}” in the top-left switcher (once per browser)")
    print()
    print(
        ui.bold("Scene 1")
        + f"  sign in as {demo.prompts.get('scene1_user', '')}, ask the copilot:\n  {ui.violet(demo.prompts.get('scene1', ''))}"
    )
    print()
    print(
        ui.bold("Scene 2")
        + f"  in iTerm:  cd {demo.shown_dir} && claude"
    )
    print("  then paste:")
    for line in demo.prompts.get("claude", "").splitlines():
        print("  " + ui.violet(line))
    print()
    ui.info(f"first time in {demo.shown_dir}: claude asks to use the folder's hexgate MCP server")
    ui.info(f"(.mcp.json): say yes; then /mcp → hexgate → Authenticate, tick “{demo.project}”")
    print()
    print(
        f"  between takes: {ui.bold('./demo/run reset ' + demo.key)}     skip scene 2: {ui.bold('./demo/run secure ' + demo.key)}"
    )


def reset(demo: Demo) -> None:
    ui.step(f"reset {demo.key}: back to the start of scene 1")
    if demo.workspace:
        sync_workspace(demo)
    else:
        shutil.copyfile(demo.ref / "agent_original.py", demo.dir / demo.agent_file)
    ui.ok(f"{demo.agent_file} restored (unprotected)")
    if not platform.is_up():
        ui.warn("platform not running: policy, registration and audit rows not touched")
        return
    api = Api()
    project_id = api.ensure_project(demo.project)
    removed = api.delete_policy_files(project_id)
    ui.ok(f"policy files deleted ({len(removed)})" if removed else "no policy files")
    forget_agent(project_id, demo.agent)
    ui.ok(f"{demo.agent} registration and audit rows deleted")
    if app_post(demo, "/api/admin/reset"):
        ui.ok("app state reset (services, chats)")
    if not demo.workspace:
        extra = sorted(p.name for p in demo.dir.iterdir()
                       if p.name not in {".env", ".venv", "__pycache__", "users.yaml", "pyproject.toml", "uv.lock",
                                         "CLAUDE.md", ".claude", "demo.toml", demo.package, demo.agent_file})
        if extra:
            ui.warn("left in the app folder by a take (check and remove by hand): " + ", ".join(extra))


def secure(demo: Demo, take: str | None = None) -> None:
    """Scene 2 without Claude: wrap, register, save the policy.

    By default the reference answer (agent_secured.py, policy/). `take` names a
    recorded answer instead, reference/<demo>/<take>/ with agent.py and
    policy/: the one a filmed Claude session wrote, so later scenes match it."""
    src = demo.ref / take if take else demo.ref
    if take and not (src / "agent.py").exists():
        raise SystemExit(f"no take {take!r}: {src}/agent.py is missing")
    ui.step(f"secure {demo.key} with {'the take ' + take if take else 'the reference answer'}")
    shutil.copyfile(src / "agent.py" if take else src / "agent_secured.py", demo.dir / demo.agent_file)
    ui.ok(f"{demo.agent_file} wrapped with HexgateRunner")
    env = {**os.environ, **platform.tool_env(), **read_env(demo.dir / ".env")}
    r = subprocess.run(
        [str(demo.dir / ".venv/bin/hexgate"), "register", "--agent", demo.register],
        cwd=demo.dir,
        env=env,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        ui.fail("hexgate register failed:\n" + (r.stderr or r.stdout)[-1500:])
        raise SystemExit(1)
    ui.ok(f"{demo.agent} registered")
    put_policy(demo, src / "policy")


def put_policy(demo: Demo, policy: Path) -> None:
    """Replace the project's policy files with the ones under `policy`."""
    api = Api()
    project_id = api.ensure_project(demo.project)
    api.delete_policy_files(project_id)
    # Imported files first: policy.yaml doesn't link until what it imports exists.
    files = sorted(
        p.relative_to(policy).as_posix()
        for p in policy.rglob("*.yaml")
        if p.name != "policy.yaml"
    )
    for name in [*files, "policy.yaml"]:
        api.put_policy_file(project_id, name, (policy / name).read_text())
    ui.ok(f"policy saved ({len(files) + 1} files)")


def status() -> None:
    ui.step("status")
    for name in ("api", "mcp", "collector", "enricher", *[d.proc for d in all_demos()]):
        pid = procs.running(name)
        print(
            f"  {name:<12} {ui.green('running') if pid else ui.dim('stopped')}"
            + (f"  pid {pid}" if pid else "")
        )
    print(
        f"  {'docker':<12} {ui.green('running') if platform.docker_ready() else ui.dim('stopped')}"
    )
    print(
        f"  {'openai key':<12} {ui.green('set in demo/.env') if openai_key_set() else ui.yellow('missing (demo/.env)')}"
    )
