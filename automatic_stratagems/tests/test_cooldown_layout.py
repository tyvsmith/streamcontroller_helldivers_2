"""Replay square geometry when cooldown fill covers the lower icon."""
import unittest
from pathlib import Path
from PIL import Image
import numpy as np
from automatic_stratagems.scanner.mission_layout import complete_frames
from automatic_stratagems.scanner.mission_layout import mission_frames


class CooldownLayoutTests(unittest.TestCase):
    def test_captured_cooldown_rows(self):
        for path in sorted((Path(__file__).parent / 'fixtures/cooldown-layout').glob('*.png')):
            with self.subTest(capture=path.stem):
                image = Image.new('RGB', (5120, 2160))
                with Image.open(path) as panel:
                    image.paste(panel, (80, 140))
                frames = mission_frames(image)
                self.assertEqual(len(frames), 7)
                for frame, expected in zip(frames, (162, 267, 371, 475, 580, 684, 788)):
                    self.assertLessEqual(abs(frame[1] - expected), 2)

    def test_grid_does_not_complete_blank_or_scenery_rows(self):
        for kind in ('blank', 'white', 'horizontal', 'vertical', 'noise'):
            with self.subTest(background=kind):
                pixels = np.zeros((950, 240, 3), dtype=np.uint8)
                runs = [[104, y, 87, 87] for y in (162, 267, 371)]
                if kind == 'white':
                    pixels[460:680] = 255
                elif kind == 'horizontal':
                    pixels[475:480] = 255
                elif kind == 'vertical':
                    pixels[460:680, 104:109] = 255
                elif kind == 'noise':
                    pixels[460:680] = np.random.default_rng(4).integers(0, 256, (220, 240, 3), dtype=np.uint8)
                self.assertEqual(complete_frames(pixels, runs, 104, 87, 104.4, 130), runs)

    def test_white_background_rows_follow_resolution(self):
        image = Image.new('RGB', (5120, 2160))
        with Image.open(Path(__file__).parent / 'fixtures/cooldown-layout/m9ulsh1d.png') as panel:
            image.paste(panel, (80, 140))
        for scale in (.75, .5):
            with self.subTest(scale=scale):
                frames = mission_frames(image.resize((round(5120 * scale), round(2160 * scale))))
                self.assertEqual(len(frames), 7)
                for frame, expected in zip(frames, (162, 267, 371, 475, 580, 684, 788)):
                    self.assertLessEqual(abs(frame[1] - expected * scale), 2)


if __name__ == '__main__':
    unittest.main()
