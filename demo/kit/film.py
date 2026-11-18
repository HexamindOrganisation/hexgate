"""./demo/run film <demo> [scene id ...] [--out DIR] [--raw VIDEO] [--no-join]: film the demo video.

Each scene is a Playwright script in demo/film/<demo>/<id>-*.js. Before a
scene, the platform and the app are put in the state the scene starts from
([film] in demo.toml); after it, the raw take is encoded to <out>/<id>.mp4.
When every scene's clip exists, the title cards are rendered and the clips are
joined into <out>/<demo>.mp4 with the cards, subtitles and pace of
demo/film/<demo>/cut.json, and the voiceover clips in <out>/vo/ if there are
any (demo/FILMING.md, "Voiceover"). A "live" scene (Claude
at work) is recorded by hand; `--raw VIDEO` cuts that recording into its clip.
`render` instead of scene ids only joins the clips. How to write and check
scenes: demo/FILMING.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from kit import demos, platform, ui
from kit.api import Api, forget_agent
from kit.paths import API, DEMO, RUN

FILM = DEMO / "film"


def _node_deps() -> None:
    """The film package (Playwright) and its Chromium, once."""
    env = {**os.environ, **platform.tool_env()}
    if not (FILM / "node_modules" / "playwright-core").exists():
        subprocess.run([*platform.pnpm(), "install", "--frozen-lockfile"], cwd=FILM, env=env, check=True,
                       capture_output=True)
    stamp = RUN / "film-browsers.stamp"
    if not stamp.exists():
        with ui.spinner("installing Chromium for filming (first run only)"):
            subprocess.run([*platform.pnpm(), "run", "browsers"], cwd=FILM, env=env, check=True,
                           capture_output=True)
        stamp.write_text("ok\n")


FONTS = ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/SFNSMono.ttf", "/System/Library/Fonts/Menlo.ttc"]


def _preflight() -> None:
    """What filming needs besides the kit's own tools: fail before a take, not after."""
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise SystemExit(f"filming needs {' and '.join(missing)} (macOS: brew install ffmpeg)")
    if not all(Path(f).exists() for f in FONTS):
        raise SystemExit("the subtitles use macOS system fonts (SF, Menlo): film on macOS")


def _script(demo: demos.Demo, sid: str) -> Path | None:
    found = sorted((FILM / demo.key).glob(f"{sid}-*.js"))
    return found[0] if found else None


def _set_state(demo: demos.Demo, state: str, take: str | None) -> None:
    if state == "reset":
        demos.reset(demo)
    elif state in ("secured", "fresh"):
        if state == "fresh":
            # The decisions the scene makes are the only ones a later "after"
            # scene shows: drop earlier takes' rows (and the registration,
            # which secure() makes again).
            api = Api()
            forget_agent(api.ensure_project(demo.project), demo.agent)
            ui.ok(f"{demo.agent} audit rows from earlier takes deleted")
        demos.secure(demo, take)
        if demos.app_post(demo, "/api/admin/reset"):
            ui.ok("app state reset (services, chats)")
    elif state != "after":
        raise SystemExit(f"unknown scene state {state!r} in {demo.src / 'demo.toml'}")


def _encode(raw: str, out: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-c:v", "libx264", "-preset", "medium",
         "-crf", "17", "-pix_fmt", "yuv420p", "-r", "25", "-movflags", "+faststart", str(out)],
        check=True,
    )


def _python(*args: str) -> None:
    """The film tools need Pillow and numpy: an isolated uv run, nothing installed."""
    subprocess.run(
        ["uv", "run", "--no-project", "--python", "3.13", "--with", "pillow", "--with", "numpy",
         "python", "-I", *args],
        env={**os.environ, **platform.tool_env()},
        check=True,
    )


def run(demo: demos.Demo, ids: list[str], out: str | None, raw: str | None = None,
        join: bool = True) -> None:
    cfg = demo.film or {}
    scenes: dict[str, str] = cfg.get("scenes", {})
    if not scenes:
        raise SystemExit(f"{demo.key} has no [film] scenes in its demo.toml")
    take = cfg.get("take")
    render_only = ids == ["render"]
    todo = [] if render_only else (ids or list(scenes))
    for sid in todo:
        if sid not in scenes:
            raise SystemExit(f"no scene {sid!r}; scenes: {', '.join(scenes)}")
    caller = Path(os.environ.get("DEMO_CALLER_CWD", os.getcwd()))
    out_dir = (caller / Path(out).expanduser()).resolve() if out else RUN / "film" / demo.key
    raw = str((caller / Path(raw).expanduser()).resolve()) if raw else None
    _preflight()
    out_dir.mkdir(parents=True, exist_ok=True)
    cut = FILM / demo.key / "cut.json"

    if any(scenes[sid] != "live" for sid in todo):
        if not platform.is_up():
            raise SystemExit(f"the platform isn't running: ./demo/run {demo.key} first")
        _node_deps()
        api = Api()
        project_id = api.ensure_project(demo.project)
        env = {
            **os.environ,
            **platform.tool_env(),
            "FILM_OUT": str(out_dir / "takes"),
            "FILM_APP_URL": demo.app_url,
            "FILM_DASH_URL": API,
            "FILM_ORG_ID": api.org_id(),
            "FILM_PROJECT_ID": project_id,
        }

    for sid in todo:
        state = scenes[sid]
        if state == "live" and raw:
            # The hand-steered recording, cut by the scene's "speed" entry in cut.json.
            ui.step(f"scene {sid}: cutting {raw}")
            _python(str(FILM / "tools" / "speed.py"), str(cut), sid, raw, str(out_dir / f"{sid}.mp4"))
            ui.ok(f"{sid}.mp4")
            continue
        if state == "live":
            have = (out_dir / f"{sid}.mp4").exists()
            ui.warn(f"{sid} is a live Claude session, filmed by hand (demo/FILMING.md)"
                    + ("; keeping the clip there is" if have
                       else "; no clip yet: film it live or code a replay (FILMING.md, \"The live Claude scene\")"))
            continue
        script = _script(demo, sid)
        if not script:
            raise SystemExit(f"no script for scene {sid}: demo/film/{demo.key}/{sid}-*.js")
        ui.step(f"scene {sid}: {script.name}")
        _set_state(demo, state, take)
        try:
            r = subprocess.run(["node", str(script)], cwd=FILM, env=env, capture_output=True, text=True)
            if r.returncode != 0:
                ui.fail(f"scene {sid} failed:\n{(r.stderr or r.stdout)[-2000:]}")
                raise SystemExit(1)
            take_video = r.stdout.strip().splitlines()[-1]
            _encode(take_video, out_dir / f"{sid}.mp4")
            ui.ok(f"{sid}.mp4")
        finally:
            if state in ("secured", "fresh") and take:
                # A scene may edit the policy (04 saves one): put the take's back,
                # even when the scene failed or was interrupted.
                demos.put_policy(demo, demo.ref / take / "policy")

    if not join:
        print(f"  clips in {out_dir}; join them with ./demo/run film {demo.key} render")
        return
    missing = [sid for sid in scenes if not (out_dir / f"{sid}.mp4").exists()]
    if missing:
        ui.warn(f"not joined yet, no clip for: {', '.join(missing)}")
        print(f"  clips in {out_dir}")
        return
    _node_deps()
    with ui.spinner("rendering the title cards"):
        r = subprocess.run(["node", "lib/cards.js", str(cut), str(out_dir)], cwd=FILM,
                           env={**os.environ, **platform.tool_env()}, capture_output=True, text=True)
    if r.returncode != 0:
        ui.fail(f"title cards failed:\n{(r.stderr or r.stdout)[-1500:]}")
        raise SystemExit(1)
    voiced = len(list((out_dir / "vo").glob("*.*"))) if (out_dir / "vo").exists() else 0
    ui.step(f"joining {len(scenes)} scenes with title cards, subtitles"
            + (f" and {voiced} voiceover clips" if voiced else " (no voiceover clips)"))
    final = out_dir / f"{demo.key}.mp4"
    partial = out_dir / f"{demo.key}.partial.mp4"  # never a half-written film under the real name
    _python(str(FILM / "tools" / "render.py"), str(cut), str(out_dir), str(partial))
    partial.replace(final)
    ui.ok(str(final))
