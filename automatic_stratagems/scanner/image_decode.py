"""Decode captured images within scanner-owned size and dimension limits."""

import io

from PIL import Image

from .errors import ScanError
from .limits import MAX_ENCODED_IMAGE_BYTES, MAX_IMAGE_DIMENSION, MAX_IMAGE_PIXELS


def decode_image(encoded, source):
    if len(encoded) > MAX_ENCODED_IMAGE_BYTES:
        raise ScanError(f"{source} encoded image is too large.")
    try:
        with Image.open(io.BytesIO(encoded)) as image:
            width, height = image.size
            if (width <= 0 or height <= 0 or
                    max(width, height) > MAX_IMAGE_DIMENSION or
                    width * height > MAX_IMAGE_PIXELS):
                raise ScanError(f"{source} image dimensions are unsupported.")
            image.load()
            return image.convert("RGB")
    except ScanError:
        raise
    except (Image.DecompressionBombError, OSError, ValueError) as error:
        raise ScanError(f"{source} returned an unreadable image.") from error
