"""Burn each scene's title and subtitles in, then join the scenes into one film.

    python tools/render.py <cut.json> <clips dir> <out.mp4> [scene id ...]

The clips are <clips dir>/<id>.mp4 (what ./demo/run film writes). Titles,
subtitle lines and their times come from cut.json; times are in each clip's
own seconds, whatever its speed. A scene plays at its "rate" (default 1), and
at a range's own speed in its "fast" ranges ([start, end, speed], clip seconds),
with a "▶▶ N×" badge from 2× up. The title is the first seconds of output; a
subtitle shows only once the title is gone. Each scene fades from and to black
at the cut. Name scene ids to
render only those (to check one scene's subtitles quickly).
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1440, 900, 25
CUT_FILE, DIR, OUT = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
ONLY = set(sys.argv[4:])

TITLE_FROM, TITLE_TO = 0.3, 3.3  # title on screen
FADE = 0.25  # subtitle fade
CUT = 0.35  # fade from/to black at each cut

SCENES = json.loads(CUT_FILE.read_text())["scenes"]

SUB_FONT = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 30)
TITLE_FONT = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 52)
NUM_FONT = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", 22)
BOX = (0, 0, 0, 153)  # black, 40% transparent, sharp corners


def box_layer(text, font, pad_x, pad_y, y_center, kicker=None):
    """A transparent full-frame RGBA layer with one boxed line of text."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
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
        d.text(
            ((W - (kr - kl)) // 2 - kl, y0 + pad_y - kt),
            kicker,
            font=NUM_FONT,
            fill=(190, 178, 255, 255),
        )
    d.text(
        ((W - (r - l)) // 2 - l, y0 + pad_y + kh - t),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )
    return layer


def alpha_at(t, a, b, fade):
    if t < a or t > b:
        return 0.0
    return min(1.0, (t - a) / fade, (b - t) / fade)


def blend(base, layer, k):
    if k <= 0:
        return base
    arr = np.asarray(layer, dtype=np.float32)
    a = arr[..., 3:4] / 255.0 * k
    return base * (1 - a) + arr[..., :3] * a


def duration(p):
    return float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(p),
            ]
        )
    )


enc = subprocess.Popen(
    [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{W}x{H}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "17",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        OUT,
    ],
    stdin=subprocess.PIPE,
)
FRAME = W * H * 3
BADGE_FONT = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 26)


def badge(img, speed):
    """The "▶▶ N×" badge, top right, while a stretch plays fast."""
    d = ImageDraw.Draw(img, "RGBA")
    x, y = W - 210, 82
    d.rectangle((x, y, x + 150, y + 46), fill=(20, 18, 32, 215), outline=(165, 148, 255, 255), width=2)
    d.text((x + 18, y + 8), f"▶▶ {speed:g}×", font=BADGE_FONT, fill=(214, 206, 255, 255))


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


scenes = [s for s in SCENES if not ONLY or s["id"] in ONLY]
total = 0
for sc in scenes:
    src = DIR / f"{sc['id']}.mp4"
    frames = plan(sc, duration(src))
    title = box_layer(sc["title"], TITLE_FONT, 44, 26, H * 0.42, kicker=sc["id"])
    # [start, end, text] or [start, end, text, "top"]: top when the bottom is
    # what the viewer reads (a message being typed into the input box).
    subs = [
        (sub[0], sub[1], box_layer(sub[2], SUB_FONT, 22, 12, 70 if sub[3:] == ["top"] else H - 70))
        for sub in sc["subs"]
    ]
    dec = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-ss", str(sc["trim"]), "-i", str(src),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE,
    )
    span = len(frames) / FPS
    idx, buf = -1, None
    for i, (t, sp) in enumerate(frames):
        want = int(round((t - sc["trim"]) * FPS))
        while idx < want:
            nxt = dec.stdout.read(FRAME)
            if len(nxt) < FRAME:
                break
            buf, idx = nxt, idx + 1
        lt = i / FPS  # output time since the cut
        img = np.frombuffer(buf, np.uint8).reshape(H, W, 3).astype(np.float32)
        img = blend(img, title, alpha_at(lt, TITLE_FROM, TITLE_TO, 0.35))
        if lt > TITLE_TO:  # subtitles never sit on the title
            for a, b, layer in subs:
                k = alpha_at(t, a, b, FADE * sp)
                if k > 0:
                    img = blend(img, layer, k)
        cut = min(1.0, lt / CUT, (span - lt) / CUT)
        img *= max(0.0, cut)
        frame = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
        if sp >= 2:
            badge(frame, sp)
        enc.stdin.write(frame.tobytes())
    dec.kill()
    total += len(frames)
    print(f"{sc['id']} {sc['title']}: {span:.1f}s", flush=True)
enc.stdin.close()
enc.wait()
print(f"total {total / FPS:.1f}s -> {OUT}")
