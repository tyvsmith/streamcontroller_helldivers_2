"""One tile cropped from a source image, with views derived from that crop."""
from dataclasses import dataclass
from functools import cached_property

from PIL import Image

from .. import icon_normalization


@dataclass(frozen=True, eq=False)
class Tile:
    """An RGB crop and its source box; derived views are computed on first use and kept.

    A Tile describes one image at one size. A resized copy is a different measurement,
    so never store or reuse its views here.
    """

    box: tuple  # x, y, width, height in source pixels
    image: Image.Image  # RGB crop of box

    @classmethod
    def from_source(cls, source, box):
        x, y, width, height = box
        return cls(tuple(box), source.crop((x, y, x + width, y + height)).convert('RGB'))

    @cached_property
    def frame_category(self):
        """Color family of the frame strip, or None when the frame gives no decisive family."""
        return icon_normalization.icon_category(self.image, frame=True)
