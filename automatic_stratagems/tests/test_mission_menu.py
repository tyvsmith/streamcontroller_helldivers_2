"""Replay captured HUD panels against independently read menu contents."""
import json
import unittest
from pathlib import Path
from PIL import Image
from automatic_stratagems.scanner.stratagem_detection import catalog, detect_mission_icons

FIXTURES = Path(__file__).parent / 'fixtures' / 'mission-panels'
FULL_SCENES = FIXTURES.parent / 'steam-full-scenes'
HISTORICAL_SCENES = FIXTURES.parent / 'historical-full-scenes'
MANIFEST = json.loads((FIXTURES.parent / 'manifest.json').read_text())
MISSION_EXPECTATIONS = next(dataset['expected_ids'] for dataset in MANIFEST['datasets']
                            if dataset['id'] == 'mission-panels')
COMMON = ['Reinforce', 'SOSBeacon', 'Resupply']

class MissionMenuTests(unittest.TestCase):
    def test_paired_side_strokes_recover_cooldown_row_with_obscured_top(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        path = HISTORICAL_SCENES / 'steam-20260908115655_1.jpg'
        with Image.open(path) as image:
            self.assertEqual([row[1] for row in mission_frames(image)],
                             [162, 266, 371, 475, 580, 684, 788])

    def test_paired_side_fallback_rejects_unbounded_or_unmatched_lines(self):
        import numpy as np
        from automatic_stratagems.scanner.mission_layout import complete_frames

        x, y, size, pitch = 104, 475, 87, 104.5
        runs = [[x, row_y, size, size]
                for row_y in (162, 267, 371, 580, 684, 788)]
        for case in ('extended', 'unequal', 'one-sided', 'erased'):
            with self.subTest(case=case):
                pixels = np.zeros((1000, 512, 3), dtype=np.uint8)
                if case != 'erased':
                    start, end = ((y - 20, y + size + 20)
                                  if case == 'extended' else (y + 8, y + size - 8))
                    pixels[start:end, x + 1:x + 5] = (180, 220, 220)
                    if case != 'one-sided':
                        right = ((220, 180, 220) if case == 'unequal'
                                 else (180, 220, 220))
                        pixels[start:end, x + size - 5:x + size - 1] = right
                frames = complete_frames(pixels, runs, x, size, pitch, 140)
                self.assertFalse(any(abs(row[1] - y) < 10 for row in frames))

    def test_terrain_edge_below_menu_is_not_a_mission_row(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        with Image.open(FULL_SCENES / '20260911164816_1.jpg') as image:
            self.assertEqual(mission_frames(image), [
                [104, 162, 87, 87],
                [104, 266, 87, 87],
                [104, 371, 87, 87],
                [104, 475, 87, 87],
                [104, 580, 87, 87],
                [104, 684, 87, 87],
            ])

    def test_independent_frame_stroke_beats_adjoining_scene_edge(self):
        from PIL import ImageDraw
        from automatic_stratagems.scanner.mission_layout import mission_frames
        for stroke in (None, 'top', 'bottom', 'right'):
            with self.subTest(stroke=stroke):
                image = Image.new('RGB', (5120, 2160))
                draw = ImageDraw.Draw(image)
                for y in (162, 267, 371):
                    draw.rectangle((104, y, 190, y + 86), fill=(0, 255, 255))
                    draw.rectangle((109, y + 5, 185, y + 81), fill=(0, 0, 0))
                draw.rectangle((104, 475, 108, 561), fill=(0, 255, 255))
                if stroke == 'top':
                    draw.rectangle((104, 475, 190, 479), fill=(0, 255, 255))
                elif stroke == 'bottom':
                    draw.rectangle((104, 557, 190, 561), fill=(0, 255, 255))
                elif stroke == 'right':
                    draw.rectangle((186, 475, 190, 561), fill=(0, 255, 255))
                draw.rectangle((205, 473, 800, 474), fill=(255, 255, 255))
                expected = [162, 267, 371] + ([] if stroke is None else [475])
                self.assertEqual([row[1] for row in mission_frames(image)], expected)

    def test_erased_name_edge_does_not_hide_observed_frame(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        with Image.open(FIXTURES / 'mizvu8wl.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        image.paste((0, 0, 0), (225, 475, 768, 516))
        self.assertEqual([row[1] for row in mission_frames(image)],
                         [162, 267, 371, 475, 580, 684, 788])

    def test_supported_horizontal_translations_preserve_observed_rows(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        with Image.open(FIXTURES / '18n2fofu.png') as panel:
            for offset in (-30, -15, -7, 0, 7, 15, 22, 30):
                with self.subTest(offset=offset):
                    image = Image.new('RGB', (5120, 2160))
                    image.paste(panel, (offset, 0))
                    frames = mission_frames(image)
                    self.assertEqual([row[0] for row in frames], [104 + offset] * 7)
                    for row, expected_y in zip(frames, (162, 267, 371, 475, 580, 684, 789)):
                        self.assertLessEqual(abs(row[1] - expected_y), 1)

    def test_competing_border_tracks_abstain(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        with Image.open(FIXTURES / '18n2fofu.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        competing = image.crop((104, 0, 191, 1100))
        image.paste(competing, (134, 0))
        self.assertEqual(mission_frames(image), [])

    def test_unsupported_safe_area_translation_abstains(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        with Image.open(FIXTURES / '18n2fofu.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (45, 0))
        self.assertEqual(mission_frames(image), [])

    def test_background_replays(self):
        for path in sorted(FIXTURES.glob('*.png')):
            with self.subTest(capture=path.stem):
                with Image.open(path) as panel:
                    image = Image.new('RGB', (5120, 2160))
                    image.paste(panel, (0, 0))
                expected = MISSION_EXPECTATIONS[path.name]
                self.assertEqual([r['id'] for r in detect_mission_icons(image, catalog())], expected)

    def test_short_bright_border_keeps_its_detected_top(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        with Image.open(FIXTURES / 'wf31myxj.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        self.assertEqual(mission_frames(image)[-1], [104, 788, 87, 87])

    def test_row_spacing_does_not_invent_an_erased_icon(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        with Image.open(FIXTURES / '9qxupakw.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        image.paste((0, 0, 0), (95, 573, 202, 678))
        frames = mission_frames(image)
        self.assertEqual(len(frames), 6)
        self.assertFalse(any(abs(row[1] - 580) < 20 for row in frames))

    def test_pale_frames_follow_resolution(self):
        from automatic_stratagems.scanner.mission_layout import mission_frames
        with Image.open(FIXTURES / '18n2fofu.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        for scale in (1, .75, .5):
            with self.subTest(scale=scale):
                resized = image.resize((round(5120 * scale), round(2160 * scale)))
                frames = mission_frames(resized)
                self.assertEqual(len(frames), 7)
                for frame, y in zip(frames, (162, 267, 371, 475, 580, 684, 788)):
                    self.assertLessEqual(abs(frame[1] - y * scale), 2)

    def test_dim_first_border_is_recovered_above_grid_anchors(self):
        from PIL import ImageDraw
        from automatic_stratagems.scanner.mission_layout import mission_frames
        image = Image.new('RGB', (5120, 2160))
        draw = ImageDraw.Draw(image)
        for y in (162, 267, 371, 475):
            draw.rectangle((104, y, 190, y + 86), fill=(0, 255, 255))
            draw.rectangle((109, y + 5, 185, y + 81), fill=(0, 0, 0))
        draw.rectangle((104, 162, 108, 176), fill=(0, 0, 0))
        frames = mission_frames(image)
        self.assertEqual(len(frames), 4)
        self.assertLessEqual(abs(frames[0][1] - 162), 1)

    def test_contrast_does_not_override_conflicting_evidence(self):
        from unittest.mock import patch
        from automatic_stratagems.scanner import stratagem_detection as detector
        image = Image.new('RGB', (5120, 2160), (200, 230, 230))
        with patch.object(detector, 'mission_frames', return_value=[[104, 162, 87, 87]]), \
             patch.object(detector, 'match_reference', return_value={'id': None}), \
             patch.object(detector, 'detect_icons', return_value=[{'id': None, 'conflict': True}]), \
             patch.object(detector, 'stretch_icon') as stretch:
            rows = detector.detect_mission_icons(image, catalog())
            self.assertIsNone(rows[0]['id'])
            stretch.assert_not_called()

    def test_blank_menu(self):
        self.assertEqual(detect_mission_icons(Image.new('RGB', (5120,2160)), catalog()), [])

    def test_unreadable_framed_row_is_retained(self):
        with Image.open(FIXTURES / 'hisdp3xc.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        # Erase the Warp Pack symbol and name, keeping its colored border.
        image.paste((0, 0, 0), (111, 587, 185, 660))
        image.paste((0, 0, 0), (230, 580, 768, 675))
        rows = detect_mission_icons(image, catalog())
        self.assertEqual(len(rows), 7)
        self.assertIsNone(rows[4]['id'])

    def test_icon_survives_erased_name_and_arrows(self):
        with Image.open(FIXTURES / 'hisdp3xc.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        image.paste((0, 0, 0), (230, 580, 768, 675))
        rows = detect_mission_icons(image, catalog())
        self.assertEqual(rows[4]['id'], 'WarpPack')
        self.assertEqual(rows[4]['method'], 'mission-icon')

    def test_obscured_icons_without_names(self):
        with Image.open(FIXTURES / 'd8iwkkqu.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        image.paste((0, 0, 0), (230, 140, 768, 920))
        rows = detect_mission_icons(image, catalog())
        self.assertEqual([r['id'] for r in rows], COMMON + ['OneTrueFlag',
            'BreakthroughExosuit', 'AntiPersonnelMinefield', 'AntiMaterialRifle'])

    def test_game_references_reject_other_catalog_icons(self):
        from automatic_stratagems.scanner.mission_references import match_reference
        from automatic_stratagems.scanner.stratagem_detection import ROOT
        entries = catalog()
        for key in entries:
            with self.subTest(icon=key), Image.open(ROOT / 'assets/icons' / f'{key}.png') as icon:
                match = match_reference(icon, entries)
                self.assertIn(match['id'], (None, key))

    def test_game_references_do_not_override_unrelated_live_tiles(self):
        import json
        from automatic_stratagems.scanner.mission_references import match_reference
        folder = FIXTURES.parent / 'live-tiles'
        entries = catalog()
        for case in json.loads((folder / 'labels.json').read_text()):
            with self.subTest(tile=case['file']), Image.open(folder / case['file']) as tile:
                match = match_reference(tile, entries)
                self.assertIn(match['id'], (None, case['expected']))
