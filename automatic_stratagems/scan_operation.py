"""Typed ownership for one coordinator scan operation."""

from dataclasses import dataclass
from threading import Event, Lock
from types import MappingProxyType
from weakref import ref


def _readonly(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _readonly(item)
                                 for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_readonly(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_readonly(item) for item in value)
    return value


@dataclass(frozen=True)
class ScanPlan:
    context: object
    session: object
    token: int
    backend: str
    workers: int
    source_snapshot: object
    image_source: object
    source_action: object
    cached_path: object
    scan_revision: int
    new_page: bool
    regenerate: bool
    attempt_id: object

    def __post_init__(self):
        object.__setattr__(self, 'source_snapshot', _readonly(self.source_snapshot))
        object.__setattr__(self, 'image_source', _readonly(self.image_source))
        object.__setattr__(self, 'source_action', _readonly(self.source_action))

    def mutable_image_source(self):
        return None if self.image_source is None else dict(self.image_source)


class ScanOperation:
    """Own events, worker, presentation, and exactly-once completion."""

    def __init__(self, initiator, *, continue_scan, on_complete):
        if not callable(on_complete):
            raise TypeError('Scan operation completion callback must be callable')
        self.cancel = Event()
        self.setup_done = Event()
        self.continuation = Event()
        self.continue_scan = bool(continue_scan)
        self.worker = None
        self.started = False
        self.plan = None
        self._initiator = ref(initiator)
        self._on_complete = on_complete
        self._completion_lock = Lock()
        self._completed = False

    def initiator(self):
        return self._initiator()

    def bind_plan(self, plan):
        if self.plan is not None:
            raise RuntimeError('Scan operation plan is already bound')
        if not isinstance(plan, ScanPlan):
            raise TypeError('Invalid scan operation plan')
        self.plan = plan

    def bind_worker(self, worker):
        if self.worker is not None:
            raise RuntimeError('Scan operation worker is already bound')
        self.worker = worker

    def mark_started(self):
        self.started = True
        self.setup_done.set()

    def complete(self):
        with self._completion_lock:
            if self._completed:
                return False
            self._completed = True
        self._on_complete(self)
        return True

    def bind_presentation(self):
        if self.plan is None:
            raise RuntimeError('Scan operation plan is unavailable')
        action = self.initiator()
        if action is None:
            return False
        action._scan_presentation = (
            self.plan.context, self.plan.session, self.plan.token)
        return True

    @staticmethod
    def presentation(action):
        return getattr(action, '__dict__', {}).get('_scan_presentation')

    @staticmethod
    def clear_presentation(action):
        action._scan_presentation = None

    @classmethod
    def action_presentation_is_active(cls, action, context, *, session=None,
                                      clear_stale=False):
        binding = cls.presentation(action)
        valid = (isinstance(binding, tuple) and len(binding) == 3
                 and binding[0] == context
                 and (session is None or binding[1] is session))
        if valid:
            try:
                valid = binding[1].is_active(binding[2])
            except Exception:
                valid = False
        if not valid and clear_stale and binding is not None:
            cls.clear_presentation(action)
        return valid
