# The Hexgate video style

How a Hexgate demo film looks and sounds. It follows the style of the Hexgate
quickstart videos (the hexgate.ai look: near-black, a navy glow, light-blue
accents), applied to films of real apps. [FILMING.md](../FILMING.md) is how to
make one; this is what it should look like.

## Shape of a film

- 1440×900, 25 fps, H.264 (yuv420p), AAC 192 kb/s, `+faststart`.
- Four to five minutes, in parts of 30–75 s. Each part opens on a title card,
  then plays the filmed app, with a voice telling what happens and a short
  subtitle line on screen.
- One part, one idea: the problem, the fix, the platform, the policy, the
  payoff, the audit.

## Palette

| token | value | use |
|---|---|---|
| bg | `#08090d` | card background |
| ink | `#f4f5f9` | headlines, emphasis |
| body | `#a9adb8` | lead lines |
| mono | `#7994c3` | eyebrows |
| dim | `#6b7080` | secondary text ("by Hexamind") |
| blue | `#4a87ea` | primary accent |
| blue-l1 → blue-l2 | `#7aa6f2` → `#a8cbfd` | the headline gradient, "gate" in the wordmark, the logo |
| red | `#ff6b6b` | the problem: denies, errors |
| green | `#4ade80` | the fix: allows, success |

Fonts: **Inter** for text, **JetBrains Mono** for eyebrows and code, with
ligatures off, so `->` and `==` show as typed.

## Title cards

Rendered from `film/lib/title-card.html`: the card's data sits in the scene's
`card` in `cut.json`.

```json
"card": { "eyebrow": "THE PROBLEM", "tone": "red",
          "lines": ["The unsafe", "copilot"],
          "lead": "One chat message can ship a bad build **straight to prod**." }
```

- **Background:** bg, a radial navy glow at the top (`#1b2a52` → `#121a33`), a
  64 px grid of 1 px `#ffffff08` lines fading out towards the edges.
- **Brand,** top left: the hexagon logo, "Hex**gate**" (gate in blue-l1), a
  thin divider, "by Hexamind" in dim.
- **Eyebrow:** a dot in the tone, then `PART <id> · <EYEBROW>` in JetBrains
  Mono, letter-spaced, in mono blue. Two to three words.
- **Headline:** two lines, Inter bold about 100 px, tight letter-spacing. The
  first line in ink; the **last line in the blue gradient, underlined** by a
  bar in the tone. The headline is the part's name, the last line its key
  words.
- **Lead:** one sentence in body, the key words in **bold** ink. It says why
  the part matters, not what happens in it.
- **Tone:** `red` for the problem, `green` for every part after the fix,
  `blue` for neutral parts.
- **Motion:** the eyebrow, each headline line and the lead rise in one by one
  (fade plus a 26 px slide, 0.5 s, staggered about 0.2 s); the underline
  draws from the left; a slow push of 1.00 → 1.035 over the card. The card
  holds 3.3 s, then dissolves into the playing scene over 0.6 s.

## On screen during a part

- **Subtitles:** one short line, white on a 60 % black box with sharp corners,
  bottom centre (top when the bottom is what the viewer reads). They are the
  scenario's subtitle lines: shorter than the voice, never more than what is
  on screen.
- **Cursor and zoom:** every action is shown by the cursor; the camera zooms
  on what the voice is talking about and comes back out before the content
  changes.
- **Speed:** the waits play faster with a "▶▶ N×" badge, top right.

## Voice

- **Voice:** ElevenLabs, model `eleven_v4`, voice **Lauren B**
  (`DODLEQrClDo8wCz460ld`) in English. One clip per voiceover line; of two
  takes, the first.
- **Text:** the scenario's voiceover line, read as a person says it: numbers
  in words ("thirty percent", "two point four"), identifiers spoken
  ("refund order", "hexgate dot A I"). That spoken form is the line's `say`;
  the subtitles keep the written form ("30%", "v2.4.0").
- **Writing it:** plain, present tense, about 2.5 words a second. A part
  opens by naming itself ("First, the unsafe copilot."), then says what the
  viewer sees as it happens. Each line lands on the beat it names.
- **Mix:** each clip at its line's start, loudness-normalised to −16 LUFS
  (true peak −1.5 dB), 48 kHz stereo. The narration also goes in the file as
  a soft subtitle track, a sentence per cue.
