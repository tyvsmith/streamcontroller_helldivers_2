import importlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import Mock, patch

from .package_loader import plugin_module


class Deck:
    def __init__(self, serial='deck-one'):
        self.serial = serial

    def serial_number(self):
        return self.serial


class SessionRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(sys.modules, {
                'loguru': types.SimpleNamespace(logger=Mock())}):
            cls.mod = importlib.import_module(
                plugin_module('automatic_stratagems.session_registry'))

    def setUp(self):
        self.plugin = types.SimpleNamespace(
            PATH='/tmp/plugin', stratagems={'A': ['UP']})
        self.deck = Deck()

    def action(self, settings=None, page='/tmp/HD2.json', deck=None):
        return types.SimpleNamespace(
            deck_controller=self.deck if deck is None else deck,
            page=types.SimpleNamespace(json_path=page),
            get_settings=lambda: dict({'group': 'HD2'} if settings is None
                                      else settings))

    def stored_registry(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return self.mod.SessionRegistry(self.plugin, state_dir=directory.name)

    def matched(self, session):
        token = session.begin([1])
        session.finish(token, {'status': 'matched', 'rows': [{'id': 'A'}]},
                       self.plugin.stratagems)
        return session

    def test_context_is_the_deck_page_and_trimmed_group(self):
        registry = self.mod.SessionRegistry(self.plugin)
        for settings, group in (({'group': ' HD2 '}, 'HD2'), ({}, 'default'),
                                ({'group': '  '}, 'default')):
            with self.subTest(settings=settings):
                self.assertEqual(registry.context(self.action(settings)),
                                 (self.deck, '/tmp/HD2.json', group))

    def test_identity_uses_the_serial_absolute_page_and_group(self):
        registry = self.mod.SessionRegistry(self.plugin)
        action = self.action(page='pages/HD2.json')
        expected = dict(deck='deck-one', page=str(Path('pages/HD2.json').absolute()),
                        group='HD2')
        self.assertEqual(registry.identity(action), expected)
        self.assertEqual(registry.context_identity(registry.context(action)), expected)

    def test_identity_requires_a_deck_serial(self):
        registry = self.mod.SessionRegistry(self.plugin)
        for serial in ('', None):
            action = self.action(deck=Deck(serial))
            with self.subTest(serial=serial):
                with self.assertRaisesRegex(ValueError, 'Deck serial number unavailable'):
                    registry.identity(action)
                with self.assertRaisesRegex(ValueError, 'Deck serial number unavailable'):
                    registry.context_identity(registry.context(action))

    def test_each_context_has_one_session_without_a_store(self):
        registry = self.mod.SessionRegistry(self.plugin)
        action = self.action()
        session = registry.session(action)
        self.assertIsNone(registry.store)
        self.assertIs(registry.session_for(registry.context(action)), session)
        self.assertIs(registry.sessions[registry.context(action)], session)
        self.assertIsNot(registry.session(self.action({'group': 'other'})), session)
        self.assertEqual(registry.state_errors, {})

    def test_persistence_without_a_store_does_nothing(self):
        registry = self.mod.SessionRegistry(self.plugin)
        action = self.action()
        with patch.object(registry, 'write_state') as write:
            registry.persist(action, self.matched(registry.session(action)))
        write.assert_not_called()
        self.assertEqual(registry.state_errors, {})

    def test_a_persisted_session_restores_in_a_new_registry(self):
        registry = self.stored_registry()
        action = self.action()
        registry.persist(action, self.matched(registry.session(action)))

        restored = self.mod.SessionRegistry(
            self.plugin, state_dir=registry.store.directory)

        self.assertEqual(restored.session(action).snapshot().assignments[1], 'A')
        self.assertEqual(restored.state_errors, {})

    def test_unreadable_state_is_recorded_and_replaced_with_a_fresh_session(self):
        registry = self.stored_registry()
        action = self.action()
        registry.store.path(registry.identity(action)).write_text('{')

        with patch.object(self.mod, 'log') as log:
            session = registry.session(action)

        self.assertFalse(session.snapshot().assignments)
        error = registry.state_errors[registry.context(action)]
        self.assertTrue(error.startswith('Unable to restore scan state: '))
        log.warning.assert_called_once_with(error)

    def test_a_failed_save_is_recorded_until_a_later_save_succeeds(self):
        registry = self.stored_registry()
        action = self.action()
        context = registry.context(action)
        session = self.matched(registry.session(action))

        with patch.object(registry.store, 'save', side_effect=OSError('disk full')), \
             patch.object(self.mod, 'log') as log:
            registry.persist_context(context, session)

        self.assertEqual(registry.state_errors[context],
                         'Unable to save scan state: disk full')
        log.warning.assert_called_once_with('Unable to save scan state: disk full')
        self.assertEqual(session.snapshot().assignments[1], 'A')
        registry.persist_context(context, session)
        self.assertNotIn(context, registry.state_errors)

    def test_write_state_saves_catalog_colors_and_localized_names(self):
        registry = self.mod.SessionRegistry(self.plugin)
        registry.store = Mock()
        session = object()
        identity = dict(deck='deck-one', page='/tmp/HD2.json', group='HD2')
        with patch.object(self.mod, 'catalog_colors',
                          return_value={'A': 'red'}) as colors:
            registry.write_state(identity, session)
            self.plugin.lm = types.SimpleNamespace(get=lambda key: key.upper())
            registry.write_state(identity, session)

        colors.assert_called_with('/tmp/plugin', self.plugin.stratagems)
        self.assertEqual(registry.store.save.call_args_list[0].args,
                         (identity, session, self.plugin.stratagems, {'A': 'red'}, {}))
        self.assertEqual(registry.store.save.call_args_list[1].args,
                         (identity, session, self.plugin.stratagems, {'A': 'red'},
                          {'A': 'ACTIONS.A.NAME'}))


if __name__ == '__main__':
    unittest.main()
