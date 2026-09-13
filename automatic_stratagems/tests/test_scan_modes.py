import unittest
from unittest.mock import patch

from PIL import Image

from automatic_stratagems.scanner import scan_game
from automatic_stratagems.scanner.errors import ScanError
from automatic_stratagems.scanner.selection_layout import selection_boxes


class SelectionGeometryTests(unittest.TestCase):
    def geometry(self, band, size):
        from automatic_stratagems.scanner.layout.geometry import SelectionGeometry
        image = Image.new('RGB', size)
        boxes = selection_boxes(image, band)
        return image, boxes, SelectionGeometry.for_band(band, boxes)

    def test_scales_to_the_calibrated_ready_bar_from_the_topmost_tile(self):
        from automatic_stratagems.scanner.layout.geometry import READY_BAR_PX
        self.assertEqual(READY_BAR_PX, 840)
        _, boxes, geometry = self.geometry([100, 800, 420, 42], (1600, 900))
        self.assertEqual(geometry.scale, 2.0)
        self.assertEqual((geometry.origin_x, geometry.origin_y), (100, min(b[1] for b in boxes)))

    def test_origin_uses_every_box_it_is_given(self):
        from automatic_stratagems.scanner.layout.geometry import SelectionGeometry
        geometry = SelectionGeometry.for_band([10, 500, 840, 84], [[10, 300, 5, 5], [20, 200, 5, 5]])
        self.assertEqual((geometry.origin_x, geometry.origin_y, geometry.scale), (10, 200, 1.0))

    def test_source_boxes_round_trip_through_local_pixels(self):
        for band, size in (([100, 800, 420, 42], (1600, 900)),
                           ([750, 1854, 840, 84], (5120, 2160)),
                           ([200, 3000, 1680, 168], (6000, 3400))):
            with self.subTest(band=band):
                _, boxes, geometry = self.geometry(band, size)
                local = geometry.to_local(boxes)
                self.assertEqual(len(local), len(boxes))
                for index, (source, mapped) in enumerate(zip(boxes, local)):
                    back = [mapped[0] / geometry.scale + geometry.origin_x,
                            mapped[1] / geometry.scale + geometry.origin_y,
                            mapped[2] / geometry.scale, mapped[3] / geometry.scale]
                    for expected, actual in zip(source, back):
                        self.assertLessEqual(abs(expected - actual), .5 / geometry.scale + 1e-9)
                    if index >= 7:
                        self.assertLessEqual(abs(mapped[2] - 150), 1)

    def test_crop_resizes_the_tile_area_above_the_band(self):
        image, _, geometry = self.geometry([100, 800, 420, 42], (1600, 900))
        image.paste((255, 0, 0), (100, geometry.origin_y, 110, geometry.origin_y + 10))
        crop = geometry.crop(image)
        self.assertEqual(crop.size, (840, round((800 - geometry.origin_y) * 2)))
        self.assertEqual(crop.getpixel((8, 8)), (255, 0, 0))

    def test_normalizes_the_band_by_image_size(self):
        from automatic_stratagems.scanner.layout.geometry import normalized_band
        self.assertEqual(normalized_band(Image.new('RGB', (1000, 500)), [100, 250, 333, 50]),
                         [.1, .5, .333, .1])


class AutoModeTests(unittest.TestCase):
    band = [100, 800, 420, 42]
    boxes = [[index, 0, 10, 10] for index in range(11)]

    def resolve(self, occupied, *, band_error=None, boxes_error=None):
        calls = []

        def empty_tile(im, box, frame_occupancy=False):
            calls.append((box[0], frame_occupancy))
            return box[0] not in occupied

        with patch.object(scan_game, 'find_selection_band', side_effect=band_error,
                          return_value=self.band), \
                patch.object(scan_game, 'selection_boxes', side_effect=boxes_error,
                             return_value=self.boxes), \
                patch.object(scan_game, 'empty_tile', side_effect=empty_tile):
            return scan_game.resolve_auto_mode(Image.new('RGB', (10, 10))), calls

    def test_missing_or_unusable_ready_bar_resolves_to_mission(self):
        self.assertEqual(self.resolve(set(), band_error=ScanError('no bar')), (('mission', None), []))
        self.assertEqual(self.resolve(set(), boxes_error=ScanError('clipped')), (('mission', None), []))

    def test_needs_two_occupied_top_tiles(self):
        result, calls = self.resolve({0})
        self.assertEqual(result, ('mission', None))
        self.assertEqual(calls, [(index, True) for index in range(7)])
        self.assertEqual(self.resolve({0, 6})[0], ('selection', self.band))

    def test_equipped_tiles_do_not_count(self):
        self.assertEqual(self.resolve({0, 7, 8, 9, 10})[0], ('mission', None))


class ModeDispatchTests(unittest.TestCase):
    def test_any_mode_other_than_selection_runs_mission_recognition(self):
        with patch.object(scan_game, 'detect_mission_icons', return_value=[]) as icons:
            report = scan_game.detect(Image.new('RGB', (100, 100)), 'unlisted')
        icons.assert_called_once()
        self.assertEqual(report, {'mode': 'unlisted', 'status': 'no_detections', 'rows': [],
                                  'layout': {'icon_region': [0, 0, .15, .53], 'calibrated': True},
                                  'warnings': [scan_game.MISSION_WARNING]})


if __name__ == '__main__':
    unittest.main()
