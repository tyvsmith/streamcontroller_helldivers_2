import importlib
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .package_loader import plugin_module
execution = importlib.import_module(plugin_module('stratagem_execution'))


class RecordingInput:
    def __init__(self):
        self.events = []
        self.syn_count = 0
        self.fail_syn = None
        self.fail_release = None

    def write(self, kind, code, value):
        if value == 0 and code == self.fail_release:
            self.fail_release = None
            raise OSError('release failed')
        self.events.append((code, value))

    def syn(self):
        self.syn_count += 1
        if self.syn_count == self.fail_syn:
            raise OSError('sync failed')


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.plugin = SimpleNamespace(
            ui=RecordingInput(), input_lock=threading.Lock(), executing=False,
            hero_mode=False, get_key_delay=lambda: 0.03,
            get_modifier_key=lambda: 'KEY_LEFTCTRL',
            get_hold_modifier=lambda: True,
            get_direction_key_layout=lambda: 'arrow_keys',
        )
        self.sleeper = patch.object(execution, 'sleep')
        self.sleep = self.sleeper.start()
        self.addCleanup(self.sleeper.stop)
        logger = patch.object(execution.log, 'exception')
        logger.start()
        self.addCleanup(logger.stop)

    def run_sequence(self, sequence=('UP', 'DOWN', 'LEFT', 'RIGHT')):
        result = execution.execute_stratagem(self.plugin, 'Test', sequence)
        self.assertFalse(self.plugin.executing)
        self.assertFalse(self.plugin.input_lock.locked())
        return result

    def test_arrow_keys_with_held_modifier(self):
        self.assertTrue(self.run_sequence())
        self.assertEqual(self.plugin.ui.events,
                         [(29, 1), (103, 1), (103, 0), (108, 1), (108, 0),
                          (105, 1), (105, 0), (106, 1), (106, 0), (29, 0)])
        self.assertTrue(all(call.args == (0.03,) for call in self.sleep.call_args_list))

    def test_wasd_with_tapped_modifier(self):
        self.plugin.get_direction_key_layout = lambda: 'wasd'
        self.plugin.get_hold_modifier = lambda: False
        self.assertTrue(self.run_sequence())
        self.assertEqual(self.plugin.ui.events,
                         [(29, 1), (29, 0), (17, 1), (17, 0), (31, 1), (31, 0),
                          (30, 1), (30, 0), (32, 1), (32, 0)])

    def test_hero_omits_modifier(self):
        self.plugin.hero_mode = True
        self.assertTrue(self.run_sequence(['UP']))
        self.assertEqual(self.plugin.ui.events, [(103, 1), (103, 0)])

    def test_settings_are_snapshotted_before_input(self):
        def change_settings(_):
            self.plugin.hero_mode = True
            self.plugin.get_direction_key_layout = lambda: 'wasd'
        self.sleep.side_effect = change_settings
        self.assertTrue(self.run_sequence(['UP', 'DOWN']))
        self.assertEqual(self.plugin.ui.events,
                         [(29, 1), (103, 1), (103, 0), (108, 1), (108, 0), (29, 0)])

    def test_sync_failure_releases_direction_and_modifier(self):
        self.plugin.ui.fail_syn = 2
        self.assertFalse(self.run_sequence(['UP']))
        self.assertEqual(self.plugin.ui.events, [(29, 1), (103, 1), (103, 0), (29, 0)])

    def test_release_failure_is_retried_and_does_not_skip_modifier(self):
        self.plugin.ui.fail_release = 103
        self.assertFalse(self.run_sequence(['UP']))
        self.assertEqual(self.plugin.ui.events, [(29, 1), (103, 1), (103, 0), (29, 0)])

    def test_persistent_release_failure_still_releases_other_keys_and_unlocks(self):
        original_write = self.plugin.ui.write
        def fail_direction_release(kind, code, value):
            if (code, value) == (103, 0):
                raise OSError('device unavailable')
            original_write(kind, code, value)
        self.plugin.ui.write = fail_direction_release
        self.assertFalse(self.run_sequence(['UP']))
        self.assertEqual(self.plugin.ui.events, [(29, 1), (103, 1), (29, 0)])

    def test_modifier_sync_failure_still_releases_modifier(self):
        self.plugin.ui.fail_syn = 1
        self.assertFalse(self.run_sequence(['UP']))
        self.assertEqual(self.plugin.ui.events, [(29, 1), (29, 0)])

    def test_settings_failure_unlocks(self):
        def broken():
            raise ValueError('bad settings')
        self.plugin.get_key_delay = broken
        self.assertFalse(self.run_sequence())
        self.assertEqual(self.plugin.ui.events, [])

    def test_lock_contention_rejects_without_changing_owner_state(self):
        self.plugin.input_lock.acquire()
        self.plugin.executing = True
        self.assertFalse(execution.execute_stratagem(self.plugin, 'Test', ['UP']))
        self.assertTrue(self.plugin.executing)
        self.assertTrue(self.plugin.input_lock.locked())
        self.assertEqual(self.plugin.ui.events, [])
        self.plugin.input_lock.release()

    def test_invalid_sequences_send_nothing(self):
        for sequence in ([], None, 'UP', ['UP', 'INVALID'], ['up'], [['UP']]):
            with self.subTest(sequence=sequence):
                self.assertFalse(self.run_sequence(sequence))
                self.assertEqual(self.plugin.ui.events, [])

    def test_missing_device_rejects(self):
        self.plugin.ui = None
        self.assertFalse(self.run_sequence())

    def test_guard_is_checked_under_input_lock(self):
        def guard():
            self.assertTrue(self.plugin.input_lock.locked())
            return False
        self.assertFalse(execution.execute_stratagem(self.plugin, 'Test', ['UP'], guard=guard))
        self.assertEqual(self.plugin.ui.events, [])
        self.assertFalse(self.plugin.input_lock.locked())


if __name__ == '__main__':
    unittest.main()
