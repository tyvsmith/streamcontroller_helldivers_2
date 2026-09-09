from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from automatic_stratagems.visibility import ChooserCompatibilityError, update_visibility


class VisibilityTests(unittest.TestCase):
    def test_toggle_hides_only_this_plugins_automatic_actions(self):
        rows = []
        for action_id in ('net_jslay_helldivers_2::ScanStratagems',
                          'net_jslay_helldivers_2::AutoStratagems',
                          'net_jslay_helldivers_2::AutomaticStratagem',
                          'net_jslay_helldivers_2::TemporaryScanBack',
                          'net_jslay_helldivers_2::Railgun', 'other::ScanStratagems'):
            rows.append(SimpleNamespace(action_holder=SimpleNamespace(action_id=action_id), set_visible=Mock()))
        chooser = SimpleNamespace(plugin_group=SimpleNamespace(expander=[SimpleNamespace(get_rows=lambda: rows)]))
        for enabled in (False, True):
            update_visibility(chooser, enabled)
            for row in rows[:4]:
                row.set_visible.assert_called_with(enabled)
            for row in rows[4:]:
                row.set_visible.assert_not_called()

    def test_missing_private_chooser_shape_fails_without_partial_changes(self):
        valid = SimpleNamespace(
            action_holder=SimpleNamespace(
                action_id='net_jslay_helldivers_2::AutomaticStratagem'),
            set_visible=Mock())
        broken_target = SimpleNamespace(action_holder=SimpleNamespace(
            action_id='net_jslay_helldivers_2::ScanStratagems'))
        broken = SimpleNamespace(get_rows=lambda: [valid, broken_target])
        chooser = SimpleNamespace(
            plugin_group=SimpleNamespace(expander=[broken]))
        with self.assertRaises(ChooserCompatibilityError):
            update_visibility(chooser, False)
        valid.set_visible.assert_not_called()

    def test_missing_expander_api_is_reported_as_incompatible(self):
        chooser = SimpleNamespace(plugin_group=SimpleNamespace(expander=[object()]))
        with self.assertRaises(ChooserCompatibilityError):
            update_visibility(chooser, False)

    def test_missing_registered_holder_is_incompatible(self):
        row = SimpleNamespace(action_holder=SimpleNamespace(
            action_id='net_jslay_helldivers_2::AutomaticStratagem'), set_visible=Mock())
        chooser = SimpleNamespace(plugin_group=SimpleNamespace(
            expander=[SimpleNamespace(get_rows=lambda: [row])]))
        with self.assertRaises(ChooserCompatibilityError):
            update_visibility(chooser, False)
        row.set_visible.assert_not_called()
