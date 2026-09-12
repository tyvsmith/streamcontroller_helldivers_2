import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from automatic_stratagems import streamcontroller_adapter as adapter


class StreamControllerAdapterTests(unittest.TestCase):
    def test_page_action_records_sort_and_keep_matching_objects(self):
        first_object = object()
        second_object = object()
        action = SimpleNamespace(page=SimpleNamespace(
            dict={'keys': {
                '0x2': {'states': {'1': {'actions': [
                    {'id': 'other', 'settings': 'ignored'},
                    {'id': 'target', 'settings': {'slot': 2}},
                ]}}},
                '4x1': {'states': {'0': {'actions': [
                    {'id': 'target', 'settings': {'slot': 1}},
                ]}}},
            }},
            action_objects={'keys': {
                '0x2': {1: {1: second_object}},
                '4x1': {0: {0: first_object}},
            }},
        ))

        self.assertEqual(adapter.page_action_records(action, 'target'), [
            ((1, 4, 0, 0), {'slot': 1}, first_object),
            ((2, 0, 1, 1), {'slot': 2}, second_object),
        ])

    def test_page_action_records_reject_malformed_matching_records(self):
        cases = (
            SimpleNamespace(page=SimpleNamespace(
                dict={'keys': []}, action_objects={'keys': {}})),
            SimpleNamespace(page=SimpleNamespace(
                dict={'keys': {'01x2': {'states': {}}}},
                action_objects={'keys': {}})),
            SimpleNamespace(page=SimpleNamespace(
                dict={'keys': {'1x2': {'states': {'0': {'actions': [
                    {'id': 'target', 'settings': []},
                ]}}}}}, action_objects={'keys': {}})),
        )
        for action in cases:
            with self.subTest(page=action.page.dict):
                self.assertIsNone(adapter.page_action_records(action, 'target'))

    def test_page_action_records_ignore_unrelated_malformed_settings(self):
        action = SimpleNamespace(page=SimpleNamespace(
            dict={'keys': {'1x2': {'states': {'0': {'actions': [
                {'id': 'other', 'settings': []},
            ]}}}}},
            action_objects={'keys': {'1x2': {0: {}}}},
        ))
        self.assertEqual(adapter.page_action_records(action, 'target'), [])

    def test_page_topology_and_action_address_use_current_host_shape(self):
        action = SimpleNamespace(
            input_ident=SimpleNamespace(input_type='keys', json_identifier='1x2'),
            state=0, page=SimpleNamespace(
                dict={'keys': {}},
                action_objects={'keys': {'1x2': {0: {4: None}}}}))
        action.page.action_objects['keys']['1x2'][0][4] = action
        self.assertEqual(adapter.page_topology(action),
                         (action.page.dict, action.page.action_objects))
        self.assertEqual(adapter.source_action_address(action), {
            'input_type': 'keys', 'identifier': '1x2', 'state': 0, 'index': 4})

    def test_cached_page_discard_clears_host_cache(self):
        page = Mock()
        manager = SimpleNamespace(pages={'deck': {'/page': {'page': page}}})
        adapter.discard_cached_page(manager, '/page')
        page.clear_action_objects.assert_called_once_with()
        self.assertNotIn('/page', manager.pages['deck'])

    def test_media_wakeup_uses_current_private_tick_seam(self):
        wake = Mock()
        player = SimpleNamespace(_cached_needs_ticks=False, _wake_event=wake)
        self.assertTrue(adapter.wake_media_ticks(
            SimpleNamespace(media_player=player)))
        self.assertTrue(player._cached_needs_ticks)
        wake.set.assert_called_once_with()

    def test_chooser_rows_fail_closed_when_current_shape_is_missing(self):
        row = SimpleNamespace(action_holder=SimpleNamespace(action_id='id'),
                              set_visible=Mock())
        chooser = SimpleNamespace(plugin_group=SimpleNamespace(
            expander=[SimpleNamespace(get_rows=lambda: [row])]))
        self.assertEqual(adapter.chooser_action_rows(chooser), [row])
        with self.assertRaises(adapter.StreamControllerCompatibilityError):
            adapter.chooser_action_rows(SimpleNamespace())


if __name__ == '__main__':
    unittest.main()
