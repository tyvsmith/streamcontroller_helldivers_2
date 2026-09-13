from collections import Counter
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from automatic_stratagems.scanner import icon_normalization
from automatic_stratagems.scanner import stratagem_detection as detection
from automatic_stratagems.scanner.icon_normalization import icon_category, normalize_icon
from automatic_stratagems.scanner.mission_layout import mission_frames

FIXTURES = Path(__file__).parent / 'fixtures'


def mission_image():
    with Image.open(FIXTURES / 'mission-panels/wf31myxj.png') as panel:
        image = Image.new('RGB', (5120, 2160))
        image.paste(panel, (0, 0))
    return image


class FrameCategoryCounter:
    """Count frame-category reads per distinct image (size and pixels)."""

    def __init__(self):
        self.calls = Counter()
        self.original = icon_normalization.icon_category

    def __call__(self, image, *, frame=False):
        if frame:
            self.calls[image.size, image.tobytes()] += 1
        return self.original(image, frame=frame)

    def patches(self):
        return (patch.object(icon_normalization, 'icon_category', self),
                patch.object(detection, 'icon_category', self))


class TileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = mission_image()
        cls.frames = mission_frames(cls.image)

    def test_crops_once_and_caches_frame_category(self):
        from automatic_stratagems.scanner.recognize.tile import Tile
        x, y, size, _ = self.frames[0]
        original_crop = Image.Image.crop
        with patch.object(Image.Image, 'crop', autospec=True, side_effect=original_crop) as crop, \
                patch.object(icon_normalization, 'icon_category',
                             wraps=icon_normalization.icon_category) as category:
            tile = Tile.from_source(self.image, (x, y, size, size))
            first_image, second_image = tile.image, tile.image
            first, second = tile.frame_category, tile.frame_category
        self.assertEqual(crop.call_count, 1)
        self.assertIs(first_image, second_image)
        category.assert_called_once_with(first_image, frame=True)
        self.assertEqual(first, second)
        self.assertEqual(tile.box, (x, y, size, size))

    def test_image_matches_todays_rgb_crop(self):
        from automatic_stratagems.scanner.recognize.tile import Tile
        for x, y, size, _ in self.frames:
            with self.subTest(frame=(x, y, size)):
                tile = Tile.from_source(self.image, (x, y, size, size))
                expected = self.image.crop((x, y, x + size, y + size)).convert('RGB')
                self.assertEqual(tile.image.mode, 'RGB')
                self.assertEqual(tile.image.size, expected.size)
                self.assertEqual(tile.image.tobytes(), expected.tobytes())
                self.assertEqual(tile.frame_category, icon_category(expected, frame=True))


class SuppliedFrameCategoryTests(unittest.TestCase):
    def tiles(self):
        image = mission_image()
        crops = [image.crop((x, y, x + size, y + size)) for x, y, size, _ in mission_frames(image)]
        # Full-size frames, their resized strip form, and a frameless tile whose category is None.
        return crops + [crop.resize((150, 150)) for crop in crops] + [Image.new('RGB', (87, 87), (90, 90, 90))]

    def test_supplied_category_is_pixel_identical_to_computed(self):
        tiles = self.tiles()
        categories = {icon_category(tile, frame=True) for tile in tiles}
        self.assertIn(None, categories)
        self.assertTrue(categories - {None})
        for index, tile in enumerate(tiles):
            with self.subTest(tile=index, size=tile.size):
                category = icon_category(tile, frame=True)
                expected = np.array(normalize_icon(tile, mission=True))
                with patch.object(icon_normalization, 'icon_category',
                                  side_effect=AssertionError('category recomputed')):
                    supplied = np.array(normalize_icon(tile, mission=True, frame_category=category))
                self.assertTrue(np.array_equal(supplied, expected))

    def test_default_still_computes_category(self):
        tile = self.tiles()[0]
        with patch.object(icon_normalization, 'icon_category',
                          wraps=icon_normalization.icon_category) as category:
            normalize_icon(tile, mission=True)
        category.assert_called_once_with(tile, frame=True)


class FrameCategoryReuseTests(unittest.TestCase):
    def test_enhancement_reads_each_full_size_tile_category_once(self):
        image = mission_image()
        frames = mission_frames(image)[:2]
        counter = FrameCategoryCounter()
        first, second = counter.patches()
        with first, second, \
                patch.object(detection, 'mission_frames', return_value=frames), \
                patch.object(detection, 'icon_occluded', return_value=False), \
                patch.object(detection, 'match_reference',
                             side_effect=lambda *args: {'id': None, 'method': 'mission-icon'}), \
                patch.object(detection, 'stretch_icon', return_value=None), \
                patch.object(detection, 'detect_icons',
                             side_effect=lambda im, entries, boxes, **kwargs: [{'id': None} for _ in boxes]), \
                patch('automatic_stratagems.scanner.colorless_icons.match_colorless',
                      side_effect=lambda *args: {'id': None}):
            rows = detection.detect_mission_icons(image, detection.catalog())
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('native_normalized_attempt' in row for row in rows))
        full_size = {key: count for key, count in counter.calls.items() if key[0] == (87, 87)}
        self.assertEqual(len(full_size), 2)
        self.assertEqual(set(full_size.values()), {1})

    def test_mission_normalization_reuses_the_strip_category(self):
        # A red-framed blank tile: the frame gives a category, the blank glyph stays unresolved.
        tile = Image.new('RGB', (150, 150), (40, 40, 40))
        tile.paste((255, 0, 0), (0, 0, 8, 150))
        entries = dict(list(detection.catalog().items())[:3])
        counter = FrameCategoryCounter()
        first, second = counter.patches()
        with first, second:
            row = detection.detect_icons(tile, entries, [[0, 0, 150, 150]], mission=True)[0]
        self.assertIsNone(row['id'])
        self.assertEqual(row['icon_category'], 'red')
        self.assertIn('normalized_attempt', row)
        self.assertEqual(counter.calls[tile.size, tile.tobytes()], 1)


if __name__ == '__main__':
    unittest.main()
