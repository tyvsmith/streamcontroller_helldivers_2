"""Read color families and draw uncertainty badges from existing button PNGs."""
from collections import Counter
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SLOT_COLORS = ('any', 'red', 'blue', 'green', 'yellow')


@lru_cache(maxsize=512)
def icon_color(path):
    with Image.open(path) as source:
        image = source.convert('RGBA')
        hsv = image.convert('RGB').convert('HSV')
        counts = Counter()
        for (h, s, v), (_, _, _, alpha) in zip(hsv.getdata(), image.getdata()):
            if alpha < 128 or s < 60 or v < 90:
                continue
            color = ('red' if h < 31 or h >= 234 else 'yellow' if h < 54
                     else 'green' if h < 110 else 'blue' if h < 200 else None)
            if color:
                counts[color] += 1
    return counts.most_common(1)[0][0] if counts else None


def catalog_colors(root, keys):
    colors = {}
    for key in keys:
        try:
            colors[key] = icon_color(str(Path(root) / 'assets/icons' / (key + '.png')))
        except OSError:
            colors[key] = None
    return colors


@lru_cache(maxsize=128)
def badged_icon(path):
    with Image.open(path) as source:
        image = source.convert('RGBA')
    size = min(image.size)
    diameter = round(size * .25)
    margin = max(1, round(size * .02))
    left, top = image.width - diameter - margin, margin
    draw = ImageDraw.Draw(image)
    draw.ellipse((left, top, left + diameter, top + diameter), fill='#ffbf36',
                 outline='#171717', width=max(1, round(size * .012)))
    font = ImageFont.load_default(size=round(diameter * .8))
    draw.text((left + diameter / 2, top + diameter / 2), '?', font=font,
              fill='#171717', anchor='mm')
    return image
