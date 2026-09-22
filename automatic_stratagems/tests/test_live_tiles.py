import json
from pathlib import Path
import unittest
from PIL import Image
from automatic_stratagems.scanner.selection_layout import empty_tile
from automatic_stratagems.scanner.stratagem_detection import catalog,detect_icons

FIXTURES=Path(__file__).parent/'fixtures/live-tiles'
class LiveTiles(unittest.TestCase):
    def test_captured_tiles(self):
        entries=catalog()
        for case in json.loads((FIXTURES/'labels.json').read_text()):
            with self.subTest(file=case['file']):
                im=Image.open(FIXTURES/case['file']).convert('RGB')
                box=[0,0,*im.size]
                if case['mission']:
                    self.assertEqual(empty_tile(im,box,frame_occupancy=True),case['empty'])
                if not case['empty']:
                    self.assertEqual(detect_icons(im,entries,[box])[0]['id'],case['expected'])

class DebugBundles(unittest.TestCase):
    def test_repeated_directory_keeps_existing_capture(self):
        import tempfile
        from automatic_stratagems.scanner.scan_game import prepare_debug_directory
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'scan'
            self.assertEqual(prepare_debug_directory(root),root)
            (root/'capture.png').write_bytes(b'original')
            child=prepare_debug_directory(root)
            self.assertEqual(child.parent,root)
            self.assertNotEqual(child,root)
            self.assertTrue(child.is_dir())
            self.assertEqual((root/'capture.png').read_bytes(),b'original')
