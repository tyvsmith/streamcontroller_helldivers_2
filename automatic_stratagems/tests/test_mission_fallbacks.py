"""Mission fallback regressions from original HUD captures."""
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from automatic_stratagems.scanner.scan_game import detect

FIXTURES = Path(__file__).parent / 'fixtures/mission-panels'


class MissionFallbackTests(unittest.TestCase):
    def test_expired_fallback_budget_leaves_every_unresolved_row_unknown(self):
        from automatic_stratagems.scanner.mission_fallbacks import apply_mission_fallbacks
        rows = [
            {'id': None, 'box': [104, 162, 87, 87], 'method': 'mission-icon'},
            {'id': None, 'box': [104, 267, 87, 87], 'method': 'mission-icon'},
        ]
        with patch('automatic_stratagems.scanner.mission_fallbacks.subprocess.run',
                   side_effect=AssertionError('OCR started after the deadline')):
            apply_mission_fallbacks(Image.new('RGB', (5120, 2160)), rows, {},
                                    deadline=time.monotonic() - 1)
        self.assertEqual([row['id'] for row in rows], [None, None])
        self.assertEqual([row['fallback_status'] for row in rows],
                         ['deadline_exhausted', 'deadline_exhausted'])

    def test_cancelled_fallback_raises_before_ocr(self):
        from automatic_stratagems.scanner.mission_fallbacks import apply_mission_fallbacks
        cancelled = Event()
        cancelled.set()
        rows = [{'id': None, 'box': [104, 162, 87, 87], 'method': 'mission-icon'}]
        with patch('automatic_stratagems.scanner.mission_fallbacks.subprocess.run',
                   side_effect=AssertionError('OCR started after cancellation')):
            with self.assertRaises(CancelledError):
                apply_mission_fallbacks(Image.new('RGB', (5120, 2160)), rows, {},
                                        cancel_event=cancelled)

    def test_name_finishing_after_deadline_is_not_accepted(self):
        from automatic_stratagems.scanner.mission_fallbacks import read_name
        clock = [0.0]

        def ocr(command, **_kwargs):
            if command[command.index('--psm') + 1] == '13':
                clock[0] = 2.0
                text = 'TARGET'
            else:
                text = ''
            return SimpleNamespace(stdout=(f'level\tconf\ttext\n5\t99\t{text}\n').encode())

        with patch('automatic_stratagems.scanner.mission_fallbacks.time.monotonic',
                   side_effect=lambda: clock[0]), \
             patch('automatic_stratagems.scanner.mission_fallbacks.subprocess.run',
                   side_effect=ocr):
            result = read_name(Image.new('RGB', (1000, 200)), [10, 10, 87, 87],
                               {'target': {'name': 'Target'},
                                'other': {'name': 'Different'}}, deadline=1.0)
        self.assertIsNone(result['id'])
        self.assertEqual(result['name_error'], 'fallback deadline exhausted')

    def test_name_matching_finishing_after_deadline_is_not_accepted(self):
        from automatic_stratagems.scanner import mission_fallbacks
        clock = [0.0]
        original = mission_fallbacks.match_name

        def ocr(command, **_kwargs):
            text = 'TARGET' if command[command.index('--psm') + 1] == '13' else ''
            return SimpleNamespace(stdout=(f'level\tconf\ttext\n5\t99\t{text}\n').encode())

        def match(text, entries):
            result = original(text, entries)
            if text == 'TARGET':
                clock[0] = 2.0
            return result

        with patch.object(mission_fallbacks.time, 'monotonic',
                          side_effect=lambda: clock[0]), \
             patch.object(mission_fallbacks.subprocess, 'run', side_effect=ocr), \
             patch.object(mission_fallbacks, 'match_name', side_effect=match):
            result = mission_fallbacks.read_name(
                Image.new('RGB', (1000, 200)), [10, 10, 87, 87],
                {'target': {'name': 'Target'}, 'other': {'name': 'Different'}},
                deadline=1.0)
        self.assertIsNone(result['id'])
        self.assertEqual(result['name_error'], 'fallback deadline exhausted')

    def test_dim_resupply_cooldown_name_recovers_unknown_icon(self):
        with Image.open(FIXTURES.parent / 'mission-fallbacks/resupply-cooldown.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        report = detect(image, 'mission')
        self.assertEqual([row['id'] for row in report['rows']],
                         ['Reinforce', 'Resupply', 'Orbital380MMHEBarrage',
                          'AirburstRocketLauncher', 'AutocannonSentry', 'EagleAirstrike'])
        self.assertEqual(report['rows'][1]['method'], 'mission-name')
        self.assertEqual(report['status'], 'matched')

    def test_dim_resupply_without_name_remains_unknown(self):
        with Image.open(FIXTURES.parent / 'mission-fallbacks/resupply-cooldown.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        image.paste((0, 0, 0), (225, 267, 780, 310))
        report = detect(image, 'mission')
        self.assertIsNone(report['rows'][1]['id'])
        self.assertEqual(report['status'], 'partial')

    def test_cooldown_names_recover_covered_symbols(self):
        with Image.open(FIXTURES / 'mizvu8wl.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        report = detect(image, 'mission')
        self.assertEqual([row['id'] for row in report['rows']],
                         ['Reinforce', 'SOSBeacon', 'Resupply', 'BulletStorm',
                          'ExpendableAntiTank', 'ExpendableNapalm', 'RecoillessRifle'])
        self.assertEqual(report['rows'][3]['method'], 'mission-name')
        self.assertEqual(report['rows'][4]['method'], 'mission-name')

    def test_name_matching_rejects_fragments_and_conflicts(self):
        from automatic_stratagems.scanner.mission_fallbacks import match_name
        from automatic_stratagems.scanner.stratagem_detection import catalog
        entries = catalog()
        for text in ('BULLET', 'ANTI TANK', 'STORM', 'Ready E', 'BLUTEL SRTMO', 'RESUPP', 'RESUPR',
                     'BULLET STORM EXPENDABLE ANTI TANK'):
            with self.subTest(text=text):
                self.assertIsNone(match_name(text, entries)['id'])
        self.assertEqual(match_name('BULLET STORMe', entries)['id'], 'BulletStorm')
        self.assertEqual(match_name('EXPENDABLE ANTI-TANK', entries)['id'], 'ExpendableAntiTank')

    def test_original_arrows_match_complete_sequences(self):
        from automatic_stratagems.scanner.mission_fallbacks import read_arrows
        from automatic_stratagems.scanner.stratagem_detection import catalog
        with Image.open(FIXTURES / '8ph1iiv_.png') as image:
            for y, key in ((475, 'BulletStorm'), (580, 'ExpendableAntiTank'),
                           (684, 'ExpendableNapalm'), (789, 'RecoillessRifle')):
                with self.subTest(key=key):
                    result = read_arrows(image, [104, y, 87, 87], catalog())
                    self.assertEqual(result['id'], key)
                    self.assertEqual(result['observed_sequence'], catalog()[key]['sequence'])

    def test_cooldown_timer_is_not_an_arrow_sequence(self):
        from automatic_stratagems.scanner.mission_fallbacks import read_arrows
        from automatic_stratagems.scanner.stratagem_detection import catalog
        with Image.open(FIXTURES / 'mizvu8wl.png') as image:
            self.assertIsNone(read_arrows(image, [104, 475, 87, 87], catalog())['id'])

    def test_arrow_hole_and_duplicate_sequence_abstain(self):
        from automatic_stratagems.scanner.mission_fallbacks import read_arrows
        from automatic_stratagems.scanner.stratagem_detection import catalog
        with Image.open(FIXTURES / '8ph1iiv_.png') as source:
            image = source.copy()
        entries = catalog()
        entries['duplicate'] = dict(entries['BulletStorm'])
        self.assertIsNone(read_arrows(image, [104, 475, 87, 87], entries)['id'])
        image.paste((0, 0, 0), (320, 520, 357, 563))
        self.assertIsNone(read_arrows(image, [104, 475, 87, 87], catalog())['id'])

    def test_clipped_tail_cannot_turn_a_prefix_into_a_match(self):
        from automatic_stratagems.scanner.mission_fallbacks import read_arrows
        from automatic_stratagems.scanner.stratagem_detection import catalog
        with Image.open(FIXTURES / '8ph1iiv_.png') as source:
            image = source.copy()
        entries = catalog()
        entries['prefix'] = {'name': 'prefix', 'sequence': entries['BulletStorm']['sequence'][:-1]}
        image.paste((255, 255, 255), (449, 520, 490, 564))
        self.assertIsNone(read_arrows(image, [104, 475, 87, 87], entries)['id'])

    def test_arrows_recover_without_ocr_and_never_override_icon(self):
        from unittest.mock import patch
        from automatic_stratagems.scanner.mission_fallbacks import apply_mission_fallbacks
        from automatic_stratagems.scanner.stratagem_detection import catalog
        with Image.open(FIXTURES / '8ph1iiv_.png') as image:
            rows = [{'id': 'Epoch', 'box': [104, 475, 87, 87], 'method': 'mission-icon'},
                    {'id': None, 'box': [104, 580, 87, 87], 'method': 'mission-icon'}]
            with patch('subprocess.run', side_effect=FileNotFoundError('tesseract unavailable')):
                apply_mission_fallbacks(image, rows, catalog())
        self.assertEqual(rows[0], {'id': 'Epoch', 'box': [104, 475, 87, 87], 'method': 'mission-icon'})
        self.assertEqual(rows[1]['id'], 'ExpendableAntiTank')
        self.assertEqual(rows[1]['method'], 'mission-arrows')
        self.assertIn('name_error', rows[1]['name_fallback'])

    def test_erased_name_uses_arrows_in_full_pipeline(self):
        with Image.open(FIXTURES / 'mizvu8wl.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
        with Image.open(FIXTURES / '8ph1iiv_.png') as arrow_source:
            image.paste(arrow_source.crop((225, 516, 768, 565)), (225, 516))
        image.paste((0, 0, 0), (225, 475, 768, 516))
        report = detect(image, 'mission')
        self.assertEqual(report['rows'][3]['id'], 'BulletStorm')
        self.assertEqual(report['rows'][3]['method'], 'mission-arrows')

    def test_full_name_beats_contained_shorter_name(self):
        from automatic_stratagems.scanner.mission_fallbacks import match_name
        from automatic_stratagems.scanner.stratagem_detection import catalog
        self.assertEqual(match_name('HEAVY MACHINE GUN', catalog())['id'], 'HeavyMachineGun')
        self.assertIsNone(match_name('MACHINE GUN HEAVY MACHINE GUN', catalog())['id'])

    def test_arrow_geometry_scales_with_hud(self):
        from automatic_stratagems.scanner.mission_fallbacks import read_arrows
        from automatic_stratagems.scanner.stratagem_detection import catalog
        with Image.open(FIXTURES / '8ph1iiv_.png') as image:
            for scale in (.75, .5):
                with self.subTest(scale=scale):
                    resized = image.resize((round(image.width * scale), round(image.height * scale)))
                    box = [round(value * scale) for value in (104, 475, 87, 87)]
                    self.assertEqual(read_arrows(resized, box, catalog())['id'], 'BulletStorm')

    def test_rotated_arrow_does_not_make_a_sequence(self):
        from automatic_stratagems.scanner.mission_fallbacks import read_arrows
        from automatic_stratagems.scanner.stratagem_detection import catalog
        with Image.open(FIXTURES / '8ph1iiv_.png') as source:
            image = source.copy()
        arrow = image.crop((235, 525, 270, 561)).rotate(45, fillcolor=(0, 0, 0))
        image.paste(arrow, (235, 525))
        self.assertIsNone(read_arrows(image, [104, 475, 87, 87], catalog())['id'])

    def test_arrow_finishing_after_deadline_is_not_accepted(self):
        from automatic_stratagems.scanner import mission_fallbacks
        from automatic_stratagems.scanner.stratagem_detection import catalog
        clock = [0.0]
        calls = [0]
        original = mission_fallbacks.cv2.matchTemplate

        def match(*args, **kwargs):
            result = original(*args, **kwargs)
            calls[0] += 1
            if calls[0] == 48:
                clock[0] = 2.0
            return result

        with Image.open(FIXTURES / '8ph1iiv_.png') as image, \
             patch.object(mission_fallbacks.time, 'monotonic',
                          side_effect=lambda: clock[0]), \
             patch.object(mission_fallbacks.cv2, 'matchTemplate', side_effect=match):
            result = mission_fallbacks.read_arrows(
                image, [104, 475, 87, 87], catalog(), deadline=1.0)
        self.assertEqual(calls[0], 48)
        self.assertIsNone(result['id'])
        self.assertEqual(result['arrow_decision'], 'deadline_exhausted')
