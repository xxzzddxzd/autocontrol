"""Regenerate ``autocontrol/board/assets/spinner.webp`` (development only; needs Pillow).

The running icon is drawn to the Feishu icon library's ring geometry so it sits
with ``ellipse_outlined`` and friends: a 56 px canvas, outer radius 26, stroke
4.8, a 250 degree arc with round caps in #3370FF, 36 frames of 30 ms (about
33 frames a second), full alpha.

    python3 tools/make_spinner.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

BLUE = (0x33, 0x70, 0xFF)
CANVAS, CENTER, OUTER, STROKE, SPAN = 56, 28.0, 26.0, 4.8, 250
SCALE, STEPS, MS = 8, 36, 30
TARGET = Path(__file__).resolve().parents[1] / 'autocontrol' / 'board' / 'assets' / 'spinner.webp'


def frame(start: float) -> Image.Image:
    big = CANVAS * SCALE
    image = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    centre, radius, width = CENTER * SCALE, OUTER * SCALE, STROKE * SCALE
    box = (centre - radius, centre - radius, centre + radius, centre + radius)
    draw.arc(box, start=start, end=start + SPAN, fill=BLUE + (255,), width=round(width))
    middle = radius - width / 2
    for angle in (start, start + SPAN):  # round caps
        x = centre + middle * math.cos(math.radians(angle))
        y = centre + middle * math.sin(math.radians(angle))
        draw.ellipse(
            (x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=BLUE + (255,)
        )
    return image.resize((CANVAS, CANVAS), Image.LANCZOS)


def main() -> None:
    frames = [frame(index * 360 / STEPS) for index in range(STEPS)]
    frames[0].save(TARGET, format='WEBP', save_all=True, append_images=frames[1:], duration=MS,
                   loop=0, lossless=True)  # fmt: skip
    print(TARGET)


if __name__ == '__main__':
    main()
