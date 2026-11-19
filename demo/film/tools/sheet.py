"""Contact sheets of a video: a frame every N seconds, time-stamped, 16 per sheet.
This is how a take gets checked and its subtitles timed: read the sheets.

    python tools/sheet.py <video> <out prefix> [step seconds, default 3]

Writes <out prefix>-0.png, -1.png, ...
"""

import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

src, out = sys.argv[1], sys.argv[2]
step = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
dur = float(subprocess.check_output(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src]))
W, H, COLS = 480, 300, 4
font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 22)
ts = [i * step for i in range(int(dur / step) + 1)]
for page in range(0, len(ts), 16):
    chunk = ts[page:page + 16]
    sheet = Image.new("RGB", (W * COLS, H * ((len(chunk) + COLS - 1) // COLS)))
    for i, t in enumerate(chunk):
        raw = subprocess.check_output(
            ["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", src, "-frames:v", "1",
             "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        if len(raw) < W * H * 3:
            continue
        im = Image.frombytes("RGB", (W, H), raw[:W * H * 3])
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, 80, 28), fill=(255, 0, 90))
        d.text((4, 2), f"{t:.0f}", font=font, fill="white")
        sheet.paste(im, ((i % COLS) * W, (i // COLS) * H))
    sheet.save(f"{out}-{page // 16}.png")
    print(f"{out}-{page // 16}.png")
