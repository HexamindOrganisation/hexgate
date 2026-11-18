"""Render the film: title cards, subtitles, pace and voiceover, joined into one file.

    python tools/render.py <cut.json> <clips dir> <out.mp4> [scene id ...]

What it reads from <clips dir>:

- <id>.mp4, one per scene (what ./demo/run film writes);
- cards/<id>.mp4, the scene's Hexgate title card (film/lib/cards.js). Without
  one, the scene gets a plain boxed title instead;
- vo/<id>-<nn>.wav (or .mp3), the voiceover clip of the scene's nn-th "vo"
  line (01 is the first). Without any, the film is silent.

Everything else comes from cut.json; its times are in each clip's own seconds,
whatever its speed. A scene plays at its "rate" (default 1) and at a range's
own speed in its "fast" ranges, with a "▶▶ N×" badge from 2× up. The card (or
title) covers the first seconds, then the scene plays and its subtitles show.
Each scene fades from and to black at the cut. With voiceover, each clip is
placed where its line starts, the mix is loudness-normalised, and the narration
goes in as a soft subtitle track (sentence by sentence). Name scene ids to
render only those, to check one scene quickly.
"""

from __future__ import annotations

import bisect
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1440, 900, 25
FRAME = W * H * 3

CARD_HOLD, CARD_OUT = 3.3, 0.6  # the card fills the screen, then dissolves into the scene
TITLE_FROM, TITLE_TO = 0.3, 3.3  # the plain title, when there is no card
FADE = 0.25  # subtitle fade
CUT = 0.35  # fade from/to black at each cut

SUB_FONT = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 30)
TITLE_FONT = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 52)
NUM_FONT = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", 22)
BADGE_FONT = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 26)
BOX = (0, 0, 0, 153)  # black, 40% transparent, sharp corners


# ------------------------------------------------------------------ layers


class Layer:
    """A boxed text drawn once, kept as float arrays of its box only, so
    blending it costs the box's pixels, not the frame's."""

    def __init__(self, img: Image.Image):
        x0, y0, x1, y1 = img.getbbox()
        arr = np.asarray(img.crop((x0, y0, x1, y1)), dtype=np.float32)
        self.box = (y0, y1, x0, x1)
        self.rgb = arr[..., :3]
        self.a = arr[..., 3:4] / 255.0

    def blend(self, base: np.ndarray, k: float) -> None:
        if k <= 0:
            return
        y0, y1, x0, x1 = self.box
        a = self.a * k
        base[y0:y1, x0:x1] = base[y0:y1, x0:x1] * (1 - a) + self.rgb * a


def box_layer(text, font, pad_x, pad_y, y_center, kicker=None) -> Layer:
    """One boxed line of text (with an optional small line above it)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    kh = 0
    if kicker:
        kl, kt, kr, kb = d.textbbox((0, 0), kicker, font=NUM_FONT)
        kh = kb - kt + 14
        tw = max(tw, kr - kl)
    bw, bh = tw + 2 * pad_x, th + kh + 2 * pad_y
    x0, y0 = (W - bw) // 2, int(y_center - bh / 2)
    d.rectangle((x0, y0, x0 + bw, y0 + bh), fill=BOX)
    if kicker:
        d.text(((W - (kr - kl)) // 2 - kl, y0 + pad_y - kt), kicker, font=NUM_FONT, fill=(190, 178, 255, 255))
    d.text(((W - (r - l)) // 2 - l, y0 + pad_y + kh - t), text, font=font, fill=(255, 255, 255, 255))
    return Layer(img)


def badge(img: Image.Image, speed: float) -> None:
    """The "▶▶ N×" badge, top right, while a stretch plays fast."""
    d = ImageDraw.Draw(img, "RGBA")
    x, y = W - 210, 82
    d.rectangle((x, y, x + 150, y + 46), fill=(20, 18, 32, 215), outline=(165, 148, 255, 255), width=2)
    d.text((x + 18, y + 8), f"▶▶ {speed:g}×", font=BADGE_FONT, fill=(214, 206, 255, 255))


def alpha_at(t, a, b, fade):
    if t < a or t > b:
        return 0.0
    return min(1.0, (t - a) / fade, (b - t) / fade)


# ------------------------------------------------------------------ timing


def duration(p) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)])
    return float(out)


def plan(sc, end):
    """(source time, speed) of each output frame: from `trim` on, at `rate`,
    and at a range's own speed inside a `fast` range."""
    rate = sc.get("rate", 1.0)
    fast = sc.get("fast", [])
    t, out = sc["trim"], []
    while t < end - 1 / FPS:
        sp = next((f for a, b, f in fast if a <= t < b), rate)
        out.append((t, sp))
        t += sp / FPS
    return out


def out_time(frames, t) -> float:
    """Output seconds (from the scene's start) at which clip time `t` plays."""
    return bisect.bisect_left([f[0] for f in frames], t) / FPS


def decoder(path: Path, start: float = 0.0):
    return subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-ss", str(start), "-i", str(path),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE)


# ------------------------------------------------------------------ video


def render_video(scenes, clips: Path, out: str) -> list[tuple[float, list]]:
    """Encode the picture; return each scene's (output start, frame plan)."""
    enc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium",
         "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
        stdin=subprocess.PIPE)
    timeline, total = [], 0
    for sc in scenes:
        src = clips / f"{sc['id']}.mp4"
        frames = plan(sc, duration(src))
        card_file = clips / "cards" / f"{sc['id']}.mp4"
        card = decoder(card_file) if card_file.exists() else None
        title = None if card else box_layer(sc["title"], TITLE_FONT, 44, 26, H * 0.42, kicker=sc["id"])
        title_end = CARD_HOLD + CARD_OUT if card else TITLE_TO
        # [start, end, text] or [start, end, text, "top"]: top when the bottom is
        # what the viewer reads (a message being typed into the input box).
        subs = [(s[0], s[1], box_layer(s[2], SUB_FONT, 22, 12, 70 if s[3:] == ["top"] else H - 70))
                for s in sc["subs"]]
        dec = decoder(src, sc["trim"])
        span = len(frames) / FPS
        idx, buf, card_buf = -1, None, None
        for i, (t, sp) in enumerate(frames):
            want = int(round((t - sc["trim"]) * FPS))
            while idx < want:
                nxt = dec.stdout.read(FRAME)
                if len(nxt) < FRAME:
                    break
                buf, idx = nxt, idx + 1
            lt = i / FPS  # output time since the cut
            img = np.frombuffer(buf, np.uint8).reshape(H, W, 3).astype(np.float32)
            if card and lt < CARD_HOLD + CARD_OUT:
                nxt = card.stdout.read(FRAME)
                card_buf = nxt if len(nxt) == FRAME else card_buf
                if card_buf is not None:
                    k = 1.0 if lt < CARD_HOLD else 1 - (lt - CARD_HOLD) / CARD_OUT
                    c = np.frombuffer(card_buf, np.uint8).reshape(H, W, 3).astype(np.float32)
                    img = img * (1 - k) + c * k
            if title:
                title.blend(img, alpha_at(lt, TITLE_FROM, TITLE_TO, 0.35))
            if lt > title_end:  # subtitles never sit on the title
                for a, b, layer in subs:
                    layer.blend(img, alpha_at(t, a, b, FADE * sp))
            img *= max(0.0, min(1.0, lt / CUT, (span - lt) / CUT))
            frame = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
            if sp >= 2 and lt > title_end:
                badge(frame, sp)
            enc.stdin.write(frame.tobytes())
        dec.kill()
        if card:
            card.kill()
        timeline.append((total / FPS, frames))
        total += len(frames)
        print(f"{sc['id']} {sc['title']}: {span:.1f}s{' (card)' if card else ''}", flush=True)
    enc.stdin.close()
    if enc.wait() != 0:
        sys.exit(f"ffmpeg failed writing {out}")
    print(f"picture {total / FPS:.1f}s")
    return timeline


# ------------------------------------------------------------------ voiceover


def vo_lines(scenes, timeline, clips: Path):
    """(output start, clip file, text) of every voiceover line with a clip."""
    lines = []
    for sc, (start, frames) in zip(scenes, timeline):
        for n, line in enumerate(sc.get("vo", []), 1):
            clip = next((p for p in (clips / "vo" / f"{sc['id']}-{n:02d}{e}" for e in (".wav", ".mp3")) if p.exists()), None)
            if clip:
                lines.append((start + out_time(frames, line[0]), clip, line[1]))
            else:
                print(f"  no voiceover clip for {sc['id']} line {n}: {line[1][:50]}")
    for (a, clip, text), (b, _, _) in zip(lines, lines[1:]):
        if a + duration(clip) > b:
            print(f"  voice overlaps the next line by {a + duration(clip) - b:.1f}s: {text[:50]}")
    return lines


def srt_time(s: float) -> str:
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(lines, path: Path) -> None:
    """The narration, a sentence per cue: a clip's length is shared between its
    sentences by their length in characters."""
    cues = []
    for start, clip, text in lines:
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
        length, total, t = duration(clip), sum(len(s) for s in sentences), start
        for s in sentences:
            d = length * len(s) / total
            cues.append((t, t + d, s))
            t += d
    path.write_text("".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{s}\n\n" for i, (a, b, s) in enumerate(cues, 1)))


def mix(lines, length: float, path: Path) -> None:
    """Each clip at its start, mixed, normalised to -16 LUFS, 48 kHz stereo."""
    args = []
    for _, clip, _ in lines:
        args += ["-i", str(clip)]
    delays = "".join(f"[{i}]adelay={int(start * 1000)}:all=1[a{i}];" for i, (start, _, _) in enumerate(lines))
    inputs = "".join(f"[a{i}]" for i in range(len(lines)))
    graph = (f"{delays}{inputs}amix=inputs={len(lines)}:normalize=0,apad,atrim=0:{length:.3f},"
             "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[out]")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args, "-filter_complex", graph,
                    "-map", "[out]", "-ac", "2", str(path)], check=True)


# ------------------------------------------------------------------ main


def main() -> None:
    cut_file, clips, out = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    only = set(sys.argv[4:])
    scenes = [s for s in json.loads(cut_file.read_text())["scenes"] if not only or s["id"] in only]
    with tempfile.TemporaryDirectory() as tmp:
        picture = Path(tmp) / "picture.mp4"
        timeline = render_video(scenes, clips, str(picture))
        lines = vo_lines(scenes, timeline, clips)
        if not lines:
            shutil.move(str(picture), out)  # out may be on another volume
            print(f"-> {out} (no voiceover)")
            return
        audio, srt = Path(tmp) / "voice.wav", Path(tmp) / "narration.srt"
        mix(lines, duration(picture), audio)
        write_srt(lines, srt)
        r = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(picture), "-i", str(audio), "-i", str(srt),
             "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", out])
        if r.returncode != 0:
            sys.exit(f"ffmpeg failed muxing {out}")
        print(f"-> {out} ({len(lines)} voiceover lines, narration subtitles)")


if __name__ == "__main__":
    main()
