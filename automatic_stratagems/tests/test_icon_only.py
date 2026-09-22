import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from automatic_stratagems.scanner.scan_game import detect


class IconOnlyTests(unittest.TestCase):
    def test_mission_does_not_invoke_ocr(self):
        panel = Image.open(Path(__file__).parent / 'fixtures/mission-panels/d8iwkkqu.png')
        image = Image.new('RGB', (5120, 2160))
        image.paste(panel, (0, 0))
        with patch('subprocess.run', side_effect=AssertionError('OCR must not run')):
            report = detect(image, 'auto')
        self.assertEqual(report['mode'], 'mission')
        self.assertEqual(len(report['rows']), 7)

    def test_selection_without_heading_does_not_invoke_ocr(self):
        folder = Path(__file__).parent / 'fixtures/live-tiles'
        image = Image.new('RGB', (5120, 2160))
        from PIL import ImageDraw
        ImageDraw.Draw(image).rectangle((750, 1854, 1589, 1937), fill='yellow')
        from automatic_stratagems.scanner.selection_layout import selection_boxes
        for i, box in enumerate(selection_boxes(image, [750,1854,840,84])):
            x,y,w,h = box
            with Image.open(folder / f'pale-0-{i}.png') as tile:
                image.paste(tile, (x,y))
        with patch('subprocess.run', side_effect=AssertionError('OCR must not run')):
            report = detect(image, 'auto')
        self.assertEqual(report['mode'], 'selection')
        self.assertEqual(len(report['rows']), 8)

    def test_yellow_bar_without_icons_is_not_selection(self):
        from PIL import ImageDraw
        image = Image.new('RGB', (5120, 2160))
        ImageDraw.Draw(image).rectangle((750,1854,1589,1937), fill='yellow')
        report = detect(image, 'auto')
        self.assertEqual(report['mode'], 'mission')
        self.assertEqual(report['status'], 'no_detections')
