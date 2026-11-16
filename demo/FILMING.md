# Filming the demo videos

The demo videos are filmed by scripts, not by hand: Playwright drives the apps
with a visible cursor and in-page zoom, and a render step adds the titles and
subtitles and joins the scenes. A new feature or a UI change means re-running
a command, not re-recording a screen.

Three layers, from what you edit to what you run:

| layer | file | who changes it |
|---|---|---|
| the story | `SCENARIO_<DEMO>.md`: scenes, beats, subtitle lines | you |
| the compiled film | `film/<demo>/*.js` (one script per scene) and `film/<demo>/cut.json` (titles, subtitle times, speed) | Claude, with the `film-demo` skill |
| the run | `./demo/run film <demo>` | anyone |

Change the story in the scenario, then ask Claude to update the film ("update
the demo video for <change>"). The `film-demo` skill
(`.claude/skills/film-demo/`) makes it update only what changed, film, check
the frames and retime the subtitles. When only the product changed, re-run the
command: the scripts find the UI by its ids and labels.

## Run it

```bash
./demo/run devops                       # the platform and the app, once
./demo/run film devops                  # every scene, then the film
./demo/run film devops 05 06            # re-film some scenes, then the film
./demo/run film devops 04 --no-join     # re-film without joining (check it first)
./demo/run film devops render           # only join the clips
```

Clips and the film go to `demo/.run/film/<demo>/` (`--out DIR` to change it):
`<id>.mp4` per scene and `<demo>.mp4`, the film. Joining takes about 10 min.

Needs macOS (the subtitles use the system fonts SF and Menlo), `ffmpeg`, the
OpenAI key in `demo/.env` (the scripted model answers differently), and for
the live scene, [minmux](#the-live-claude-scene). Chromium for Playwright
installs itself on the first run.

## What is where

| path | what |
|---|---|
| `film/lib/film.js` | `Film`: the recorder. Cursor, clicks, typing, zoom |
| `film/lib/desktop.js` | the mock desktop an app opens from |
| `film/lib/dashboard.js` | a signed-in hexgate dashboard for a scene |
| `film/lib/terminal.js` | records a terminal app for the live Claude scene |
| `film/lib/env.js` | what the film command passes to a script |
| `film/tools/render.py` | titles, subtitles, speed, the join |
| `film/tools/speed.py` | cuts the long live recording down |
| `film/tools/sheet.py` | contact sheets: how a take is checked and timed |
| `film/devops/` | the DevOps demo: `shipyard.js` (app moves), `01-…js` to `06-…js`, `cut.json` |
| `apps/devops/demo.toml`, `[film]` | each scene's starting state, and the Claude take to restore |
| `reference/devops/claude-take/` | the `agent.py` and policy Claude wrote in the filmed session |

## How a scene script works

A scene is a Node script that opens a recorded browser, acts, and returns the
raw video:

```js
scene(async () => {
  const f = await Film.open(path.join(env.out, '05'));   // recording starts
  await openShipyard(f);                                  // the desktop intro
  await f.click('#send');                                 // glide, press, ripple
  await f.zoomTo('#badge', { scale: 2.2 });               // camera onto an element
  return f.close();                                       // the raw video
});
```

`Film` moves:

- `moveTo(target)`, `hover(target, { hold })`, `click(target)`: the cursor
  glides in an arc. A target is a selector, a locator, or `f.spot(x, y)` for a
  place with no selector (a chart bar), in page coordinates.
- `type(text, delay)`: types into the focused field.
- `zoomTo(target, { scale | maxScale, pad })`: the camera onto an element.
  `zoomToBox({x, y, width, height})`: onto a region, in page coordinates (what
  a screenshot at zoom 1 shows). `zoomToRect(rect)`: onto a rect in viewport
  coordinates, as `getBoundingClientRect` gives it now. `zoomOut()`.
- `follow(scale)`: zoom in around the cursor; the camera then trails it.
- `settle()`: after a navigation (the new page starts unzoomed).

The zoom is a CSS transform on `<body>` inside the page, so text stays sharp.
The cursor lives on `<html>`, outside the zoomed body.

Scripts find the UI by stable hooks, and the app keeps them: in Shipyard, the
ids `#ask`, `#send`, `#composer`, `#badge`, `#activity`, `#thread`, `#user`,
`#pw`, `#go`, `#logout`; `[data-svc=<service>] .cell` (staging is the 2nd
child, prod the 3rd); `.card.blocked`, `.card.approval .actions`,
`.msg-user`. In the dashboard: visible labels (tab names, button names),
scoped to `main`, since the sidebar repeats them.

## Scene states

Scenes depend on each other through the platform: 06 shows the decisions 05
made, and 03 to 06 need the copilot secured. `[film]` in `demo.toml` gives
each scene the state it starts from, and the film command sets it up:

- `reset`: the start of scene 1 (`./demo/run reset`): `agent.py`
  unprotected, no policy, no registration, no audit rows, app state reset.
- `secured`: the take restored (`./demo/run secure <demo> --take
  claude-take`): Claude's `agent.py`, registered, its policy; app state reset.
  After the scene, the take's policy is put back (04 saves an edit).
- `after`: nothing; the scene continues from the one before. Film it right
  after that one.
- `live`: filmed by hand (below).

## The live Claude scene

Scene 02 is Claude Code securing the app for real, in a terminal: nobody can
script what Claude does, so the take is steered as it runs, then cut.

1. `./demo/run reset devops`. Create `~/shipyard/.claude/settings.local.json`
   once, so the take isn't stopped by permission prompts (it is gitignored and
   survives resets):

   ```json
   {"permissions": {"allow": ["Bash(uv run:*)", "Bash(uv sync:*)", "Bash(git diff:*)",
     "Bash(git status:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)", "Edit", "Write",
     "mcp__hexgate"]}, "enabledMcpjsonServers": ["hexgate"]}
   ```
2. Start the recorder: `node film/lib/terminal.js <dir> ~/shipyard` (from
   `demo/`). It launches minmux (`MINMUX_APP`, profile `hexgate`) without this
   session's `CLAUDE*`/`ANTHROPIC*` variables, so the filmed Claude is your
   own login.
3. Steer it by appending lines to `<dir>/cmd.jsonl` (the format is at the top
   of `terminal.js`). The sequence used:
   `exec zsh -f`, then
   `PROMPT='%F{141}hexuser%f %F{117}%1~%f %F{245}❯%f '` and `clear` (a clean
   prompt), `claude`, accept the folder trust dialog, type the prompt from
   `demo.toml` with a typing delay, Enter. Take `{"shot": …}` screenshots to
   watch; `{"quit": true}` at the end.
4. Cut it. `<dir>/log.txt` has every command's time since launch. Claude's
   session log (`~/.claude/projects/-Users-…-shipyard/<session>.jsonl`)
   timestamps each step; raw video time is about log time minus the launch
   delay. Fill `speed.segments` in `cut.json`: waits at 12×, Claude's notes
   and final answer at 1× with a view zoomed on them. Then
   `./demo/run film devops 02 --raw <dir>/video/<file>.webm`.
5. Keep the take: copy `~/shipyard/agent.py` to
   `reference/devops/claude-take/agent.py` and the saved policy files to
   `reference/devops/claude-take/policy/`, so the later scenes show what
   Claude wrote.

## cut.json

```json
{ "id": "05", "title": "Same request, now blocked", "trim": 0.2,
  "rate": 1.7, "fast": [[24.0, 32.0, 3]],
  "subs": [[23.5, 32.5, "Sam sends the exact same request.", "top"]] }
```

- Times are seconds in the scene's clip (`<id>.mp4`), whatever the speed.
- `trim`: cut from the start (a browser take starts with a few white frames).
- `rate`: the scene's pace. The film aims at 4 to 5 minutes; the browser
  scenes play at about 1.6×.
- `fast`: ranges played faster, with a "▶▶ N×" badge from 2× up: typing a
  long message, a model thinking.
- `subs`: `[start, end, text]`, plus `"top"` when the bottom of the screen is
  what the viewer reads (the message being typed).
- `speed` (02 only): how the raw terminal recording is cut, see above.

Timing rules: the title is the first 3.3 s of each scene, and a subtitle shows
only after it. Keep each subtitle on screen at least 2 s of output (clip time
÷ rate). Lines are short, spoken, and never explain more than what's on screen.

## Checking a take

Read it, don't trust it: a script that ran without an error can still have
zoomed on an empty area or typed into the wrong field.

```bash
uv run --no-project --python 3.13 --with pillow python -I \
  film/tools/sheet.py .run/film/devops/05.mp4 /tmp/sheet-05 3
```

A frame every 3 s, time-stamped: check each beat of the scenario is on screen
and readable, and read the subtitle times off it. Check the film decodes:
`ffmpeg -v error -i .run/film/devops/devops.mp4 -f null -` prints nothing.

## Traps already hit

- **Zoom scrolls the page.** A transformed body makes the document scrollable,
  and a focus or caret move scrolls the window: the camera math breaks. The
  init script locks window scroll while filming.
- **Backdrop blur shows mirrored text** past a zoomed body's edges (dialogs,
  drawers). `html.__zoomed` turns `backdrop-filter` off while zoomed.
- **No cursor in the video** if the init script runs before `<html>` exists:
  it waits for `readystatechange`.
- **Chrome won't frame localhost from another origin** (local network access
  checks): the mock desktop is served on the app's own origin.
- **A failed script leaves a blank video**: Playwright only finalizes it on
  close. Fix the script and re-run the scene.
- **Two joins at once corrupt the film**: the join writes
  `<demo>.partial.mp4` and renames it, but don't start a second one.
- **The dashboard's audit times are an hour off** (11:55 UTC shown for 12:55
  UTC): a known bug, visible in 06.

## A new demo

1. `SCENARIO_<DEMO>.md`: the scenes, their beats and subtitle lines.
2. `[film]` in `apps/<demo>/demo.toml`: the scene ids and their starting states.
3. Ask Claude to film it (the `film-demo` skill): it writes
   `film/<demo>/<id>-<name>.js` from the scenario, reusing `film/lib/`, then
   films, checks and writes `cut.json`.
