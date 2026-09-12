import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch

from .package_loader import plugin_module

CONTEXT = ('deck', '/tmp/HD2.json', 'HD2')


def topology(*records):
    """Build a beta.15 page from (column, row, state, index, settings, object)."""
    data, objects = {'keys': {}}, {'keys': {}}
    for column, row, state, index, settings, action_object in records:
        identifier = f'{column}x{row}'
        key = data['keys'].setdefault(identifier, {'states': {}})
        actions = key['states'].setdefault(str(state), {'actions': []})['actions']
        while len(actions) <= index:
            actions.append({'id': 'other::Action', 'settings': {}})
        actions[index] = {'id': 'net_jslay_helldivers_2::AutomaticStratagem',
                          'settings': settings}
        objects['keys'].setdefault(identifier, {}).setdefault(state, {})[index] = action_object
    return types.SimpleNamespace(json_path=CONTEXT[1], dict=data, action_objects=objects)


class Automatic:
    def __init__(self, context=CONTEXT, slot=1, color='any', *, present=True,
                 ready=True, page=None, settings=None):
        self.context = context
        self._slot = slot
        self._color = color
        self.present = present
        self.on_ready_called = ready
        self.page = page or types.SimpleNamespace(json_path=context[1])
        self.settings = {'group': context[2]} if settings is None else settings
        self.render = Mock()

    def slot(self):
        return self._slot

    def color_filter(self):
        return self._color

    def get_is_present(self):
        return self.present

    def get_settings(self):
        return self.settings


class Other:
    """An attached action in the same context that is not an Automatic slot."""

    context = CONTEXT


class Host:
    """The coordinator surface the reconciler reaches through."""

    def __init__(self, mod, *actions):
        self.actions = list(actions)
        self.sessions = {}
        self.cancel_context = Mock()
        self.persist_context = Mock()
        self.reconciler = mod.SlotReconciler(self, Automatic)

    def _attached_actions(self):
        return list(self.actions)

    def context(self, action):
        return action.context

    def session_for(self, context):
        return self.sessions.setdefault(context, Mock())


class ModuleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(sys.modules, {
                'loguru': types.SimpleNamespace(logger=Mock())}):
            cls.mod = importlib.import_module(
                plugin_module('automatic_stratagems.slot_reconciliation'))


class SlotPolicyTests(ModuleTestCase):
    def test_configured_slots_are_positive_and_capped(self):
        for settings, slot in (({'slot': 5}, 5), ({'slot': '3'}, 3), ({'slot': 150}, 99),
                               ({'slot': 0}, -1), ({'slot': -4}, -1), ({'slot': 'x'}, -1),
                               ({'slot': None}, -1), ({}, -1)):
            with self.subTest(settings=settings):
                self.assertEqual(self.mod.configured_slot(settings), slot)

    def test_color_filters_default_to_any(self):
        self.assertEqual(self.mod.settings_color_filter({'color_filter': 'blue'}), 'blue')
        self.assertEqual(self.mod.settings_color_filter({'color_filter': 'purple'}), 'any')
        self.assertEqual(self.mod.settings_color_filter({}), 'any')

    def test_layout_is_unavailable_without_page_topology(self):
        self.assertIsNone(self.mod.resolved_layout(Automatic()))

    def test_layout_reserves_explicit_slots_then_allocates_by_row_column_state_index(self):
        group = {'group': 'HD2'}
        page = topology(
            (0, 1, 0, 0, dict(group), 'row-one'),
            (1, 0, 0, 0, dict(group, slot=1), 'explicit'),
            (0, 0, 1, 0, dict(group), 'later-state'),
            (0, 0, 0, 1, dict(group), 'later-index'),
            (0, 0, 0, 0, dict(group), 'first'),
            (2, 0, 0, 0, {'group': 'other'}, 'other-group'))
        layout = self.mod.resolved_layout(Automatic(page=page))
        self.assertEqual([(position, action_object, slot)
                          for position, _, action_object, slot in layout], [
            ((0, 0, 0, 0), 'first', 2),
            ((0, 0, 0, 1), 'later-index', 3),
            ((0, 0, 1, 0), 'later-state', 4),
            ((0, 1, 0, 0), 'explicit', 1),
            ((1, 0, 0, 0), 'row-one', 5),
        ])

    def test_layout_uses_the_requested_group_over_the_action_group(self):
        page = topology((0, 0, 0, 0, {'group': 'HD2'}, 'mine'),
                        (1, 0, 0, 0, {'group': 'other'}, 'theirs'))
        layout = self.mod.resolved_layout(Automatic(page=page), 'other')
        self.assertEqual([(action_object, slot) for _, _, action_object, slot in layout],
                         [('theirs', 1)])

    def test_layout_fails_when_no_automatic_slot_remains(self):
        records = [(slot, 0, 0, 0, {'group': 'HD2', 'slot': slot}, None)
                   for slot in range(1, 100)]
        page = topology(*records, (0, 1, 0, 0, {'group': 'HD2'}, None))
        with self.assertRaisesRegex(ValueError, 'No automatic slots remain'):
            self.mod.resolved_layout(Automatic(page=page))


class SlotReconcilerTests(ModuleTestCase):
    def test_slot_filters_come_from_attached_automatic_actions_in_the_context(self):
        host = Host(self.mod, Automatic(slot=1, color='red'), Automatic(slot=None),
                    Automatic(('deck', '/tmp/other.json', 'HD2'), slot=2), Other())
        self.assertEqual(host.reconciler.slot_filters(CONTEXT, Mock()), {1: 'red'})

    def test_slot_filters_fall_back_to_the_saved_session(self):
        host = Host(self.mod)
        session = Mock()
        session.checkpoint.return_value = {'slots': {'2': {'filter': 'blue'}}}
        self.assertEqual(host.reconciler.slot_filters(CONTEXT, session), {2: 'blue'})

    def test_configured_filters_are_authoritative_from_page_topology(self):
        action = Automatic(slot=None, present=False)
        action.page = topology(
            (0, 0, 0, 0, {'group': 'HD2', 'color_filter': 'blue'}, action),
            (1, 0, 0, 0, {'group': 'HD2', 'slot': 3, 'color_filter': 'red'}, None))
        host = Host(self.mod, action)
        self.assertEqual(host.reconciler._configured_filters(CONTEXT),
                         ({1: 'blue', 3: 'red'}, True))
        self.assertEqual(host.reconciler.configured_filters(CONTEXT), {1: 'blue', 3: 'red'})

    def test_configured_filters_without_topology_use_present_slots_only(self):
        host = Host(self.mod, Automatic(slot=1, color='red'),
                    Automatic(slot=2, present=False), Automatic(slot=None))
        self.assertEqual(host.reconciler._configured_filters(CONTEXT), ({1: 'red'}, False))

    def test_an_action_without_a_slot_is_only_bound_to_its_context(self):
        action = Automatic(slot=None)
        host = Host(self.mod, action)
        host.reconciler.reconcile_action(action)
        self.assertEqual(action._scan_context, CONTEXT)
        host.sessions[CONTEXT].reconcile.assert_not_called()

    def test_a_changed_session_is_saved_and_redrawn_and_cancelled_only_while_scanning(self):
        action = Automatic(slot=2, color='green')
        for status, cancelled in (('ready', False), ('scanning', True)):
            with self.subTest(status=status):
                host = Host(self.mod, action, Automatic(slot=1, color='red'))
                session = host.session_for(CONTEXT)
                session.snapshot.return_value.status = status
                session.reconcile.return_value = True
                with patch.object(host.reconciler, 'redraw') as redraw:
                    host.reconciler.reconcile_action(
                        action, old_context=CONTEXT, old_slot=1)
                session.reconcile.assert_called_once_with(
                    {1: 'red', 2: 'green'}, affected={1, 2})
                host.persist_context.assert_called_once_with(CONTEXT, session)
                redraw.assert_called_once_with(CONTEXT)
                self.assertEqual(host.cancel_context.called, cancelled)

    def test_a_slot_moved_from_another_context_affects_only_its_new_slot(self):
        action = Automatic(slot=2)
        host = Host(self.mod, action)
        session = host.session_for(CONTEXT)
        session.reconcile.return_value = False
        host.reconciler.reconcile_action(
            action, old_context=('deck', '/tmp/other.json', 'HD2'), old_slot=1)
        session.reconcile.assert_called_once_with({2: 'any'}, affected={2})
        host.persist_context.assert_not_called()

    def test_authoritative_topology_reconciles_every_slot(self):
        action = Automatic(slot=1)
        action.page = topology((0, 0, 0, 0, {'group': 'HD2'}, action))
        host = Host(self.mod, action)
        session = host.session_for(CONTEXT)
        session.reconcile.return_value = False
        host.reconciler.reconcile_action(action)
        session.reconcile.assert_called_once_with({1: 'any'}, affected=None)

    def test_redraw_renders_ready_present_actions_in_the_context_and_logs_failures(self):
        rendered = Automatic()
        failing = Automatic()
        failing.render.side_effect = RuntimeError('render failed')
        later = Automatic()
        skipped = [Automatic(ready=False), Automatic(present=False),
                   Automatic(('deck', '/tmp/other.json', 'HD2'))]
        host = Host(self.mod, rendered, failing, *skipped, later)
        with patch.object(self.mod, 'log') as log:
            host.reconciler.redraw(CONTEXT)
        rendered.render.assert_called_once_with()
        later.render.assert_called_once_with()
        for action in skipped:
            action.render.assert_not_called()
        log.exception.assert_called_once_with('Unable to render scan action')


if __name__ == '__main__':
    unittest.main()
