import importlib
import types
import unittest

from .package_loader import plugin_module

CONTEXT = ('deck', '/tmp/HD2.json', 'HD2')


class Session:
    def __init__(self, *active):
        self.active = set(active)

    def is_active(self, token):
        return token in self.active


class PageAttemptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module(
            plugin_module('automatic_stratagems.page_attempts'))

    def action(self):
        return types.SimpleNamespace()

    def test_attempts_are_numbered_per_action_and_start_scanning(self):
        action, other = self.action(), self.action()
        self.assertEqual(self.mod.begin_page_attempt(action), 1)
        self.assertEqual(action._scan_attempt,
                         self.mod.ScanAttempt(1, 'scanning', 'Scanning'))
        self.mod.finish_page_attempt(action, 1, 'failed', 'broken')
        self.assertEqual(self.mod.begin_page_attempt(action), 2)
        self.assertEqual(action._scan_attempt,
                         self.mod.ScanAttempt(2, 'scanning', 'Scanning'))
        other._scan_attempt = 'not an attempt'
        self.assertEqual(self.mod.begin_page_attempt(other), 1)

    def test_binding_attaches_the_session_only_to_the_current_attempt(self):
        action, session = self.action(), Session(3)
        self.assertFalse(self.mod.bind_page_attempt(action, 1, session, 3))
        attempt_id = self.mod.begin_page_attempt(action)
        self.assertFalse(self.mod.bind_page_attempt(action, attempt_id + 1, session, 3))
        self.assertIsNone(action._scan_attempt.session)
        self.assertTrue(self.mod.bind_page_attempt(action, attempt_id, session, 3))
        self.assertEqual(action._scan_attempt,
                         self.mod.ScanAttempt(attempt_id, 'scanning', 'Scanning', session, 3))

    def test_finishing_records_the_status_and_message_text(self):
        action, session = self.action(), Session(3)
        attempt_id = self.mod.begin_page_attempt(action)
        self.mod.bind_page_attempt(action, attempt_id, session, 3)
        self.assertTrue(self.mod.finish_page_attempt(
            action, attempt_id, 'failed', RuntimeError('capture failed'),
            session=session, token=3))
        self.assertEqual(action._scan_attempt, self.mod.ScanAttempt(
            attempt_id, 'failed', 'capture failed', session, 3))

    def test_finishing_ignores_a_stale_attempt_session_or_token(self):
        action, session = self.action(), Session(3)
        self.assertFalse(self.mod.finish_page_attempt(action, 1, 'failed', 'none'))
        attempt_id = self.mod.begin_page_attempt(action)
        self.mod.bind_page_attempt(action, attempt_id, session, 3)
        before = action._scan_attempt
        for arguments in ((attempt_id + 1, {}),
                          (attempt_id, {'session': Session(3), 'token': 3}),
                          (attempt_id, {'session': session, 'token': 4})):
            with self.subTest(arguments=arguments):
                self.assertFalse(self.mod.finish_page_attempt(
                    action, arguments[0], 'failed', 'stale', **arguments[1]))
                self.assertIs(action._scan_attempt, before)
        self.assertTrue(self.mod.finish_page_attempt(
            action, attempt_id, 'cancelled', 'Scan already in progress'))
        self.assertEqual(action._scan_attempt.status, 'cancelled')

    def scanning(self, session, token, presentation):
        action = self.action()
        attempt_id = self.mod.begin_page_attempt(action)
        self.mod.bind_page_attempt(action, attempt_id, session, token)
        if presentation is not None:
            action._scan_presentation = presentation
        return action

    def test_cancelling_finishes_only_attempts_presenting_the_active_session_scan(self):
        session = Session(3)
        cancelled = self.scanning(session, 3, (CONTEXT, session, 3))
        finished = self.scanning(session, 3, (CONTEXT, session, 3))
        self.mod.finish_page_attempt(finished, 1, 'failed', 'broken',
                                     session=session, token=3)
        other_session = Session(3)
        unchanged = [
            finished,
            self.scanning(other_session, 3, (CONTEXT, other_session, 3)),
            self.scanning(session, 3, (('deck', '/tmp/other.json', 'HD2'), session, 3)),
            self.scanning(session, 3, (CONTEXT, session, 4)),
            self.scanning(session, 4, (CONTEXT, session, 4)),
            self.scanning(session, 3, None),
        ]
        before = [action._scan_attempt for action in unchanged]

        self.mod.cancel_page_attempt([cancelled, *unchanged], CONTEXT, session)

        self.assertEqual(cancelled._scan_attempt, self.mod.ScanAttempt(
            1, 'cancelled', 'Scan cancelled', session, 3))
        self.assertEqual([action._scan_attempt for action in unchanged], before)


if __name__ == '__main__':
    unittest.main()
