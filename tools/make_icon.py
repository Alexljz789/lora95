#!/usr/bin/env python3
"""Generate the pixel-art LoRa95 icon (run locally; requires Pillow)."""
from pathlib import Path
from PIL import Image, ImageDraw

SIZE = 64
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Window body with a 3D bevel.
d.rectangle([1, 1, SIZE - 2, SIZE - 2], fill=(192, 192, 192, 255), outline=(10, 10, 10, 255))
d.line([2, 2, SIZE - 3, 2], fill=(255, 255, 255, 255))
d.line([2, 2, 2, SIZE - 3], fill=(255, 255, 255, 255))
d.line([SIZE - 2, 3, SIZE - 2, SIZE - 2], fill=(128, 128, 128, 255))
d.line([3, SIZE - 2, SIZE - 2, SIZE - 2], fill=(128, 128, 128, 255))

# Title bar.
d.rectangle([3, 3, SIZE - 4, 14], fill=(0, 0, 128, 255))
d.rectangle([4, 4, 8, 5], fill=(255, 255, 0, 255))
for x in range(10, 40, 3):
    d.point((x, 8), fill=(255, 255, 255, 255))

# Antenna mast and base.
d.rectangle([30, 19, 33, 42], fill=(20, 20, 20, 255))
d.rectangle([24, 42, 39, 45], fill=(20, 20, 20, 255))
d.rectangle([28, 22, 35, 24], fill=(0, 128, 0, 255))
d.rectangle([30, 25, 33, 30], fill=(0, 128, 0, 255))

# Left/right signal arcs (pixel blocks).
arcs = [
    (22, 24, 25, 27), (19, 27, 22, 30), (16, 30, 19, 33),
    (38, 24, 41, 27), (41, 27, 44, 30), (44, 30, 47, 33),
]
for x0, y0, x1, y1 in arcs:
    d.line([x0, y1, x1, y0], fill=(0, 160, 0, 255), width=2)

# Small "95" badge.
d.rectangle([40, 38, 56, 52], fill=(192, 192, 192, 255), outline=(10, 10, 10, 255))
d.rectangle([41, 39, 55, 40], fill=(255, 255, 255, 255))
try:
    from PIL import ImageFont
    font = ImageFont.truetype("arialbd.ttf", 12)
    d.text((42, 39), "95", font=font, fill=(0, 0, 0, 255))
except Exception:
    d.text((42, 40), "95", fill=(0, 0, 0, 255))

# Transparent corners.
for x in range(SIZE):
    for y in range(SIZE):
        if (x < 1 or y < 1 or x > SIZE - 2 or y > SIZE - 2):
            img.putpixel((x, y), (0, 0, 0, 0))

for path in [
    Path("icon.png"),
    Path("app/static/img/icon.png"),
    Path("docs/icon.png"),
]:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    print(path)
