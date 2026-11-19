---
name: film-demo
description: Film, re-film or update a hexgate demo video (demo/film/, `./demo/run film <demo>`) — turn a change in demo/SCENARIO_<DEMO>.md into updated scene scripts and cut.json, film the affected scenes, check every take with contact sheets, time the subtitles and the voiceover, make the voice clips (ElevenLabs), and join the film with Hexgate title cards. Use when asked to make, redo, update, shorten or fix a demo video, a scene of it, its subtitles or its titles, or to film a new demo; also when a product change (new UI, renamed button) breaks or dates the video.
---

# Film a demo video

`demo/FILMING.md` is the reference: the layers, the `Film` API, scene states,
the live Claude scene, `cut.json`, the voiceover, the traps already hit.
`demo/film/STYLE.md` is how the film must look and sound: the Hexgate title
cards, palette, subtitles and voice. Read both first. This skill is the loop
around them.

## The rule

`SCENARIO_<DEMO>.md` is the source of truth for what the video shows and says.
The scene scripts (`demo/film/<demo>/*.js`) and `cut.json` are compiled from
it, and committed so a re-run needs no model. Never improvise a take that the
scenario doesn't describe; if the user wants a different story, change the
scenario first (or propose the change), then compile.

## Loop

1. **Diff intent against the film.** Read the scenario and the current
   scripts and `cut.json`. List what changed: a beat, a subtitle line, a scene
   order, a product change the scripts must follow. Touch only those scenes.
2. **Update the scripts.** Reuse `film/lib/` and the demo's step module
   (`film/devops/shipyard.js`). Find the UI by ids and visible labels, not by
   layout classes. A new moving part of an app gets a stable id in the app
   first. Region zooms take page coordinates (`zoomToBox`, `spot`).
3. **Film without joining:** `./demo/run film <demo> <ids> --no-join`. Mind
   the states in `[film]` of `demo.toml`: a scene marked `after` needs the
   scene before it re-filmed too (06 shows 05's decisions). The live Claude
   scene is never re-filmed unless asked: it costs a real session; follow
   FILMING.md's steps for it. On a machine without `.run/film/<demo>/02-raw.webm`
   (it isn't committed), ask which way to make scene 02: a real session in a
   terminal app you can drive (minmux), or an HTML terminal replay you code
   from `reference/<demo>/claude-take/` (FILMING.md, "The live Claude scene").
4. **Check every take** with `film/tools/sheet.py` (a frame every 2–3 s) and
   read the sheets. Each beat of the scenario must be on screen and readable;
   the zoom must land on content, not on an empty area; nothing may cover what
   the viewer is reading. A take with a wrong frame gets its script fixed and
   is filmed again, not papered over in the cut.
5. **Time the subtitles** in `cut.json` from the sheets, in clip seconds.
   Each line starts when its beat is on screen, shows at least 2 s of output
   (clip time ÷ rate), and starts after the title card (trim + 3.9 × rate). A line
   over the message being typed gets `"top"`. Lines are the scenario's, word
   for word; if a line doesn't fit the picture, change it in the scenario too.
6. **Cards and voice.** Each part needs a `card` in cut.json, compiled from
   the scenario's **Card** (STYLE.md: the tone, two to three eyebrow words, a
   two-line headline whose last line carries the key words, a lead that says
   why it matters). The voice comes after the cut is final: time each
   **Voiceover** line in `vo` from the sheets, then make one clip per line
   whose text changed (FILMING.md, "Voiceover"; ElevenLabs settings in
   STYLE.md). Never remake unchanged lines: they cost credits. No ElevenLabs
   access: say so and ask for the clips, and still join the rest silent.
7. **Join:** `./demo/run film <demo> render` (about 10 min: run it in the
   background, and never two at once). Check it decodes:
   `ffmpeg -v error -i <film> -f null -` prints nothing. Look at a few frames:
   a title card, its dissolve into the scene, a subtitle at the top, a fast
   stretch with its badge. With voice, listen for a line that lands on the
   wrong beat or runs into the next (the join prints overlaps).
8. **Report** the film's path and length, what changed per scene, and anything
   off that you saw but didn't fix.

## Pace

The film aims at 4–5 minutes. Pace with `rate` per scene and `fast` ranges
(typing, a model thinking) in `cut.json` before cutting content; cut a beat
only by changing the scenario. Scene 02's waits play at 12×, Claude's own
words at 1×.

## Don't

- Don't commit videos: clips and films live in `demo/.run/film/` (gitignored).
- Don't hand-edit `reference/<demo>/claude-take/` except to store a new live
  take: the later scenes are filmed against it.
- Don't run the join while another one runs; don't film while someone uses
  the demo (filming resets the platform's state).
