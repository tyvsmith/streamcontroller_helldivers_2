import unittest

from automatic_stratagems import capture_source


class CaptureSourceTests(unittest.TestCase):
    def test_defaults_are_hotkey_steam_folder_and_delete(self):
        self.assertEqual(capture_source.screenshot_config({}), {
            'kind': 'folder', 'path': '', 'trigger': 'hotkey',
            'hotkey': 'KEY_F12', 'script': '', 'delete_after_scan': True,
        })

    def test_normalizes_invalid_values_without_extra_trigger_modes(self):
        self.assertEqual(capture_source.screenshot_config({
            'screenshot_trigger': 'none', 'screenshot_hotkey': '',
            'screenshot_script': 4, 'screenshot_folder': None,
            'screenshot_delete': 1,
        }), capture_source.screenshot_config({}))

    def test_identity_distinguishes_auto_screenshot_and_gamescope(self):
        settings = {'screenshot_folder': '/captures'}
        self.assertNotEqual(
            capture_source.screenshot_source_identity('auto', settings),
            capture_source.screenshot_source_identity('screenshot', settings))
        self.assertEqual(
            capture_source.screenshot_source_identity('gamescope', settings),
            {'backend': 'gamescope', 'source': None})

    def test_source_settings_use_action_backend_and_global_screenshot_values(self):
        merged = capture_source.source_settings(
            {'capture_backend': 'steam', 'screenshot_folder': '/ignored'},
            {'screenshot_folder': '/captures', 'screenshot_trigger': 'script',
             'screenshot_script': '/tmp/save'})
        self.assertEqual(merged['capture_backend'], 'screenshot')
        self.assertEqual(merged['screenshot_folder'], '/captures')
        self.assertEqual(merged['screenshot_trigger'], 'script')
        self.assertEqual(merged['screenshot_script'], '/tmp/save')

    def test_gui_source_settings_discard_legacy_history(self):
        settings = capture_source.source_settings({
            'capture_backend': 'screenshot',
            'image_source_last_fingerprint': 'a' * 64,
            'image_source_last_path': '/old/frame.png',
            'image_source_last_mtime_ns': 1,
        }, {'screenshot_folder': '/old'})

        for key in ('image_source_last_fingerprint', 'image_source_last_path',
                    'image_source_last_mtime_ns'):
            self.assertNotIn(key, settings)
            self.assertNotIn(key, capture_source.page_source_settings(settings))
        self.assertIsNone(capture_source.operation_source(
            settings, allow_rescan=True)['previous_fingerprint'])

    def test_source_arguments_serialize_without_shell_interpretation(self):
        source = {
            'kind': 'folder', 'path': '/captures/a ; $(touch nope)',
            'trigger': 'hotkey', 'hotkey': 'KEY_F12', 'script': '',
            'delete_after_scan': True, 'previous_fingerprint': 'a' * 64,
            'allow_rescan': True,
        }
        arguments = capture_source.source_arguments(source)
        self.assertEqual(arguments[arguments.index('--image-source-path') + 1],
                         source['path'])
        self.assertIn('--delete-screenshot', arguments)
        self.assertIn('--allow-image-rescan', arguments)


if __name__ == '__main__':
    unittest.main()
