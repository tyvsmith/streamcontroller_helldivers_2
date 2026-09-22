"""Calibrated selection geometry: the tile area above a Ready bar, in matcher pixels."""
from dataclasses import dataclass

# Selection tile ratios were measured on an 840-pixel Ready bar; matching rescales to it.
READY_BAR_PX = 840


@dataclass(frozen=True)
class SelectionGeometry:
    """Maps source pixels above one Ready bar to the calibrated image the matcher reads."""

    band: list
    origin_x: int
    origin_y: int
    scale: float

    @classmethod
    def for_band(cls, band, boxes):
        """boxes must be every selection tile for band, empty tiles included."""
        return cls(band, band[0], min(b[1] for b in boxes), READY_BAR_PX / band[2])

    def crop(self, im):
        """Crop im to the tile area above the band and resize it to the calibrated scale."""
        crop = im.crop((self.origin_x, self.origin_y, self.band[0] + self.band[2], self.band[1]))
        return crop.resize((round(crop.width * self.scale), round(crop.height * self.scale)))

    def to_local(self, boxes):
        """Convert source-pixel boxes to the local pixels of the image crop() returns.

        Each coordinate is rounded independently, so round-tripping is approximate.
        """
        return [[round((x - self.origin_x) * self.scale), round((y - self.origin_y) * self.scale),
                 round(w * self.scale), round(h * self.scale)] for x, y, w, h in boxes]


def normalized_band(im, band):
    """Express band as fractions of im's width and height, rounded to 6 decimals."""
    return [round(n / size, 6) for n, size in zip(band, (im.width, im.height, im.width, im.height))]
