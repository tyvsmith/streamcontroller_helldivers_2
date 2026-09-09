import unittest
from pathlib import Path
from PIL import Image, ImageChops
from automatic_stratagems.scan_artwork import icon_color, badged_icon, catalog_colors

ROOT=Path(__file__).resolve().parents[2]

class ArtworkTests(unittest.TestCase):
    def test_existing_pngs_supply_all_four_colors(self):
        expected={'Reinforce':'yellow','Railgun':'blue','EagleStrafingRun':'red','MachineGunSentry':'green'}
        self.assertEqual(catalog_colors(ROOT,expected),expected)

    def test_badge_changes_only_top_right_and_leaves_original_untouched(self):
        path=str(ROOT/'assets/icons/Railgun.png')
        before=Path(path).read_bytes()
        with Image.open(path) as original:
            original=original.convert('RGBA')
        result=badged_icon(path)
        box=ImageChops.difference(original,result).convert('RGB').getbbox()
        self.assertIsNotNone(box)
        self.assertGreater(box[0],original.width*.65)
        self.assertLess(box[3],original.height*.3)
        self.assertEqual(Path(path).read_bytes(),before)
