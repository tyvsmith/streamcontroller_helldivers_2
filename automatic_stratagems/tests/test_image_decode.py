import unittest
from unittest.mock import MagicMock, patch

from automatic_stratagems.scanner import image_decode


class DecodeImagePrimitivesTests(unittest.TestCase):
    def test_encoded_image_limit_is_checked_before_open(self):
        with patch.object(image_decode, 'MAX_ENCODED_IMAGE_BYTES', 8), \
             patch.object(image_decode.Image, 'open') as open_image, \
             self.assertRaisesRegex(image_decode.ScanError,
                                    'encoded image is too large'):
            image_decode.decode_image(b'123456789', 'test')
        open_image.assert_not_called()

    def test_pixel_limit_is_checked_before_load_or_conversion(self):
        source = MagicMock()
        source.size = (100_000, 100_000)
        source.__enter__.return_value = source
        with patch.object(image_decode.Image, 'open', return_value=source), \
             self.assertRaisesRegex(image_decode.ScanError, 'dimensions'):
            image_decode.decode_image(b'png', 'test')
        source.load.assert_not_called()
        source.convert.assert_not_called()


if __name__ == '__main__':
    unittest.main()
