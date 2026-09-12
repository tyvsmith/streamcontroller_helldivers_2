"""Ready-bar localization fallbacks and rejection boundaries."""

from pathlib import Path
import unittest

from PIL import Image, ImageDraw

from automatic_stratagems.scanner.game_capture import ScanError
from automatic_stratagems.scanner.selection_layout import (
    find_selection_band,
    selection_boxes,
)


FIXTURES = Path(__file__).parent / 'fixtures'


def washed_out_panel(size=(2000, 900), *, edge_y=None, occupied=True,
                     panel_start=None, panel_end=None, band_width=None):
    width, height = size
    panel_end = round(width * .31) if panel_end is None else panel_end
    band_width = (round(width * 840 / 5120)
                  if band_width is None else band_width)
    panel_start = (panel_end - round(band_width * 984 / 840)
                   if panel_start is None else panel_start)
    edge_y = round(height * .897) if edge_y is None else edge_y
    band_height = round(band_width * 84 / 840)
    band = [panel_end - band_width, edge_y - band_height + 1,
            band_width, band_height]

    image = Image.new('RGB', size)
    draw = ImageDraw.Draw(image)
    pale = (255, 236, 225)
    draw.rectangle((band[0], band[1], panel_end - 1, edge_y - 1), fill=pale)
    draw.line((panel_start, edge_y - 1, panel_end - 1, edge_y - 1),
              fill=pale, width=1)
    if occupied:
        for box in selection_boxes(image, band)[:2]:
            x, y, box_width, box_height = box
            draw.rectangle((x, y, x + box_width - 1, y + box_height - 1),
                           outline=(230, 190, 80), width=max(3, box_width // 10))
    return image, band


class WashedOutReadyBarTests(unittest.TestCase):
    def test_complete_washed_out_left_panel_recovers_ready_bar(self):
        cases = ({}, {'band_width': 260, 'panel_end': 760})
        for options in cases:
            with self.subTest(options=options):
                image, expected = washed_out_panel(**options)
                actual = find_selection_band(image)
                for value, wanted in zip(actual, expected):
                    self.assertAlmostEqual(value, wanted, delta=2)

    def test_half_scale_real_capture_preserves_ready_bar_geometry(self):
        path = (FIXTURES / 'hdr-full-scenes' /
                'screenshot-2026-09-08_12-21-37.png')
        with Image.open(path) as original:
            image = original.resize((2561, 1081), Image.Resampling.LANCZOS)
        band = find_selection_band(image)
        for value, expected in zip(band, [376, 928, 421, 42]):
            self.assertAlmostEqual(value, expected, delta=2)

    def test_isolated_pale_bar_is_not_complete_panel_evidence(self):
        image, band = washed_out_panel(occupied=False)
        image = Image.new('RGB', image.size)
        ImageDraw.Draw(image).rectangle(
            (band[0], band[1], band[0] + band[2] - 1,
             band[1] + band[3] - 1),
            fill=(255, 236, 225))
        with self.assertRaises(ScanError):
            find_selection_band(image)

    def test_panel_edge_without_occupied_tiles_is_rejected(self):
        image, _ = washed_out_panel(occupied=False)
        with self.assertRaises(ScanError):
            find_selection_band(image)

    def test_competing_complete_panel_edges_are_ambiguous(self):
        image, _ = washed_out_panel(edge_y=807)
        competing, _ = washed_out_panel(edge_y=770)
        image.paste(competing, mask=competing.convert('L'))
        with self.assertRaises(ScanError):
            find_selection_band(image)

    def test_competing_yellow_bars_do_not_enter_fallback(self):
        image = Image.new('RGB', (2000, 900))
        draw = ImageDraw.Draw(image)
        for x in (100, 450):
            draw.rectangle((x, 775, x + 328, 804), fill='yellow')
        with self.assertRaises(ScanError):
            find_selection_band(image)

    def test_right_player_panel_is_not_local_player_evidence(self):
        image, _ = washed_out_panel(
            panel_start=1100, panel_end=1482)
        with self.assertRaises(ScanError):
            find_selection_band(image)

    def test_clipped_left_panel_is_not_complete_panel_evidence(self):
        image, _ = washed_out_panel(
            panel_start=0, panel_end=580, band_width=495)
        with self.assertRaises(ScanError):
            find_selection_band(image)

    def test_closed_menu_and_capture_dialog_remain_negative(self):
        paths = [
            FIXTURES / 'steam-full-scenes' / '20260911164153_1.jpg',
            FIXTURES / 'hdr-full-scenes' / 'screenshot-2026-09-10_19-48-31.png',
        ]
        for path in paths:
            with self.subTest(path=path.name), Image.open(path) as image:
                with self.assertRaises(ScanError):
                    find_selection_band(image)


class SelectionLayoutTests(unittest.TestCase):
    def test_four_players_choose_complete_leftmost_bar(self):
        image = Image.new('RGB', (2000, 900))
        draw = ImageDraw.Draw(image)
        for x in [290, 650, 1010, 1370]:
            draw.rectangle((x, 775, x + 329, 804), fill=(255, 255, 0))
        band = find_selection_band(image)
        self.assertAlmostEqual(band[0], 290, delta=3)
        self.assertGreater(band[0] + band[2], image.width / 4)
        boxes = selection_boxes(image, band)
        self.assertEqual(len(boxes), 11)
        self.assertTrue(all(x + width <= band[0] + band[2]
                            for x, _, width, _ in boxes))

    def test_no_bar_is_explicit_failure(self):
        with self.assertRaises(ScanError):
            find_selection_band(Image.new('RGB', (2000, 900)))

    def test_invalid_manual_boundary_is_rejected(self):
        with self.assertRaises(ScanError):
            selection_boxes(Image.new('RGB', (2000, 900)), [0, 50, 500, 30])

    def test_right_player_only_is_not_selected(self):
        image = Image.new('RGB', (2000, 900))
        ImageDraw.Draw(image).rectangle((1000, 775, 1329, 804), fill='yellow')
        with self.assertRaises(ScanError):
            find_selection_band(image)


if __name__ == '__main__':
    unittest.main()
