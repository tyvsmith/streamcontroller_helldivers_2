"""Record each action's generated-page scan attempt across scan operations."""
from dataclasses import dataclass

from .scan_operation import ScanOperation


@dataclass(frozen=True)
class ScanAttempt:
    id: int
    status: str
    message: str
    session: object = None
    token: int | None = None


def begin_page_attempt(action):
    previous = getattr(action, '__dict__', {}).get('_scan_attempt')
    attempt = ScanAttempt((previous.id if isinstance(previous, ScanAttempt) else 0) + 1,
                          'scanning', 'Scanning')
    action._scan_attempt = attempt
    return attempt.id


def bind_page_attempt(action, attempt_id, session, token):
    attempt = getattr(action, '__dict__', {}).get('_scan_attempt')
    if not isinstance(attempt, ScanAttempt) or attempt.id != attempt_id:
        return False
    action._scan_attempt = ScanAttempt(
        attempt.id, attempt.status, attempt.message, session, token)
    return True


def finish_page_attempt(action, attempt_id, status, message,
                        *, session=None, token=None):
    attempt = getattr(action, '__dict__', {}).get('_scan_attempt')
    if not isinstance(attempt, ScanAttempt) or attempt.id != attempt_id:
        return False
    if session is not None and (attempt.session is not session or attempt.token != token):
        return False
    action._scan_attempt = ScanAttempt(
        attempt.id, status, str(message), attempt.session, attempt.token)
    return True


def cancel_page_attempt(actions, context, session):
    for action in actions:
        attempt = getattr(action, '__dict__', {}).get('_scan_attempt')
        presentation = ScanOperation.presentation(action)
        if (isinstance(attempt, ScanAttempt) and attempt.status == 'scanning'
                and attempt.session is session and presentation is not None
                and presentation[:2] == (context, session)
                and attempt.token == presentation[2]
                and session.is_active(attempt.token)):
            finish_page_attempt(
                action, attempt.id, 'cancelled', 'Scan cancelled',
                session=session, token=attempt.token)
