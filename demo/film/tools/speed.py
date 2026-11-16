"""Cut a long live recording (Claude at work in a terminal) down to a watchable
clip: the waiting plays fast with a "▶▶ N×" badge, what Claude says plays at 1×
with the camera zoomed on it.

    python tools/speed.py <cut.json> <scene id> <raw video> <out.mp4>

The cut is the scene's "speed" entry in cut.json: segments are
[start, end, speed, view] in raw-video seconds, keeping only what they cover;
a view names one of "views", [center x, center y, zoom], or null for the full
frame. Views ease into each other over EASE seconds.
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

CUT_FILE, SCENE, SRC, OUT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
W, H, FPS = 1440, 900, 25
EASE = 0.7  # seconds for the camera to move from one view to the next

_spec = next(s for s in json.loads(Path(CUT_FILE).read_text())["scenes"] if s["id"] == SCENE)["speed"]
VIEWS = _spec["views"]
SEGMENTS = [(a, b, sp, tuple(VIEWS[v]) if v else None) for a, b, sp, v in _spec["segments"]]

def view_box(v):
    if v is None:
        return (W / 2, H / 2, 1.0)
    return v


def clamp(cx, cy, s):
    w, h = W / s, H / s
    x = min(max(cx - w / 2, 0), W - w)
    y = min(max(cy - h / 2, 0), H - h)
    return x, y, w, h


def ease(t):
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


# Output frame plan: (source time, view, speed)
plan = []
prev = view_box(None)
for start, end, speed, v in SEGMENTS:
    target = view_box(v)
    n = int(round((end - start) / speed * FPS))
    ease_n = min(int(EASE * FPS), max(1, n // 2))
    for i in range(n):
        k = ease(min(1.0, (i + 1) / ease_n))
        cur = tuple(a + (b - a) * k for a, b in zip(prev, target))
        plan.append((start + i * speed / FPS, cur, speed))
    prev = target

font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 26)


def badge(img, speed):
    d = ImageDraw.Draw(img, "RGBA")
    text = f"▶▶ {speed}×"
    x, y = W - 210, 82
    d.rounded_rectangle((x, y, x + 150, y + 46), radius=12, fill=(20, 18, 32, 215), outline=(165, 148, 255, 255), width=2)
    d.text((x + 18, y + 8), text, font=font, fill=(214, 206, 255, 255))


dec = subprocess.Popen(
    ["ffmpeg", "-loglevel", "error", "-i", SRC, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
    stdout=subprocess.PIPE,
)
enc = subprocess.Popen(
    ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
     "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT],
    stdin=subprocess.PIPE,
)
FRAME = W * H * 3
idx, frame = -1, None
for t, (cx, cy, s), speed in plan:
    want = int(round(t * FPS))
    while idx < want:
        buf = dec.stdout.read(FRAME)
        if len(buf) < FRAME:
            break
        frame, idx = buf, idx + 1
    img = Image.frombuffer("RGB", (W, H), frame)
    if s > 1.001:
        x, y, w, h = clamp(cx, cy, s)
        img = img.resize((W, H), Image.LANCZOS, box=(x, y, x + w, y + h))
    else:
        img = img.copy()
    if speed > 1:
        badge(img, speed)
    enc.stdin.write(np.asarray(img, dtype=np.uint8).tobytes())
enc.stdin.close()
enc.wait()
dec.kill()
print(f"{len(plan)} frames, {len(plan) / FPS:.1f}s")
