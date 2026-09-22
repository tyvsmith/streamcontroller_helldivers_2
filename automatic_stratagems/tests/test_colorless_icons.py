import unittest
from pathlib import Path
from PIL import Image
import numpy as np
from automatic_stratagems.scanner.colorless_icons import match_colorless
from automatic_stratagems.scanner.stratagem_detection import catalog, ROOT
from automatic_stratagems.scanner.mission_fallbacks import match_name
from automatic_stratagems.scanner.capture_backends import capture_issue

FIXTURES = Path(__file__).parent/'fixtures/colorless-cooldown'

class ColorlessTests(unittest.TestCase):
    def test_raw_cooldown_icons_without_names_or_arrows(self):
        for path in FIXTURES.glob('*.png'):
            with self.subTest(path=path.name), Image.open(path) as image:
                self.assertEqual(match_colorless(image,catalog())['id'],path.stem.split('-',1)[1])

    def test_vertical_overlay_levels_preserve_identity(self):
        for key in ['Meltagun','BastionMkXvi','Railgun','MachineGunSentry']:
            with Image.open(ROOT/'assets/icons'/f'{key}.png') as im:
                glyph=im.convert('RGB').crop((23,23,121,121)).resize((77,77))
            for fraction in [0,.1,.3,.5,.7,.9,1]:
                a=np.asarray(glyph).copy()
                gray=a.max(2)
                pale=140+gray.astype(float)/255*100
                count=round(77*fraction)
                a[:count]=np.repeat(pale[:count,:,None],3,axis=2)
                tile=Image.new('RGB',(87,87),'black');tile.paste(Image.fromarray(a.astype('uint8')),(5,5))
                with self.subTest(key=key,fraction=fraction):
                    result = match_colorless(tile,catalog())['id']
                    if key == 'MachineGunSentry':
                        # Its silhouette resembles Gatling Sentry; abstention is acceptable.
                        self.assertIn(result, (None, key))
                    else:
                        self.assertEqual(result,key)

    def test_blank_and_overlay_without_detail_stay_unknown(self):
        for color in ['white','gray','black']:
            self.assertIsNone(match_colorless(Image.new('RGB',(87,87),color),catalog())['id'])

    def test_shortened_model_names_and_ocr_model_noise(self):
        for text,key in [('meltagun','Meltagun'),('MELTAGUN !','Meltagun'),
                         ('BASTION MR XVUI','BastionMkXvi'),('BASTION MR XULo','BastionMkXvi')]:
            self.assertEqual(match_name(text,catalog())['id'],key)
        for text in ['PACK','GUN','BAST','MELTA']:
            self.assertIsNone(match_name(text,catalog())['id'])

    def test_ambiguous_model_free_name_is_not_accepted(self):
        entries={'a':{'name':'40-K Meltagun'},'b':{'name':'50-K Meltagun'}}
        self.assertIsNone(match_name('MELTAGUN',entries)['id'])
        self.assertEqual(match_name('40-K MELTAGUN',entries)['id'],'a')

    def test_uniform_capture_rejected(self):
        self.assertIsNotNone(capture_issue(Image.new('RGB',(512,216),'gray'),'gamescope'))

    def test_false_color_capture_is_rejected_without_recognition_scores(self):
        root = Path(__file__).parent/'fixtures/capture-quality'
        with Image.open(root/'steam-corrupt-preview.png') as image:
            self.assertIn('false-color',capture_issue(image,'steam'))
        with Image.open(root/'gamescope-valid-preview.png') as image:
            self.assertIsNone(capture_issue(image,'steam'))
            self.assertIsNone(capture_issue(image,'gamescope'))
