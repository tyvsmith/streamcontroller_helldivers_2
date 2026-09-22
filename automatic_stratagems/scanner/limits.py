"""Size limits for captured images, shared by capture, decoding, and file reads.

This module imports nothing so the command runner, the plugin process, and
decoders can all read the same limits without loading an image library.
"""

MAX_ENCODED_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 40_000_000
