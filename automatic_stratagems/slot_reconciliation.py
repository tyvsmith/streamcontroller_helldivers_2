"""Resolve Automatic slots from page topology and reconcile their color filters."""
from loguru import logger as log

from .scan_session import SLOT_COLORS
from .session_registry import scan_group
from .streamcontroller_adapter import page_action_records


AUTOMATIC_ACTION_ID = 'net_jslay_helldivers_2::AutomaticStratagem'


def configured_slot(settings):
    try:
        value = int(settings.get('slot', -1))
    except (ValueError, TypeError):
        return -1
    return min(value, 99) if value > 0 else -1


def resolved_layout(action, group=None):
    records = page_action_records(action, AUTOMATIC_ACTION_ID)
    if records is None:
        return None
    group = scan_group(action.get_settings()) if group is None else group
    records = [record for record in records if scan_group(record[1]) == group]
    reserved = {configured_slot(settings) for _, settings, _ in records}
    reserved.discard(-1)
    available = (slot for slot in range(1, 100) if slot not in reserved)
    resolved = []
    for position, settings, action_object in records:
        slot = configured_slot(settings)
        if slot == -1:
            try:
                slot = next(available)
            except StopIteration:
                raise ValueError('No automatic slots remain') from None
        resolved.append((position, settings, action_object, slot))
    return resolved


def settings_color_filter(settings):
    value = settings.get('color_filter', 'any')
    return value if value in SLOT_COLORS else 'any'


class SlotReconciler:
    """Keep each context's session slots and filters in step with its Automatic actions.

    The coordinator supplies attached actions, contexts, sessions, scan
    cancellation, and persistence; each is looked up on it when used.
    """

    def __init__(self, coordinator, automatic_type):
        self.coordinator = coordinator
        self.automatic_type = automatic_type

    def slot_filters(self, context, session):
        slots = [a for a in self.coordinator._attached_actions()
                 if isinstance(a, self.automatic_type)
                 and self.coordinator.context(a) == context]
        if slots:
            return {slot: action.color_filter() for action in slots
                    if (slot := action.slot()) is not None}
        return {int(slot): row['filter'] for slot, row in session.checkpoint()['slots'].items()}

    def configured_filters(self, context):
        filters, _ = self._configured_filters(context)
        return filters

    def _configured_filters(self, context):
        candidates = [candidate for candidate in self.coordinator._attached_actions()
                      if isinstance(candidate, self.automatic_type)
                      and self.coordinator.context(candidate) == context]
        for candidate in candidates:
            layout = resolved_layout(candidate, context[2])
            if layout is not None:
                return ({slot: settings_color_filter(settings)
                         for _, settings, _, slot in layout}, True)
        return ({slot: candidate.color_filter() for candidate in candidates
                 if candidate.get_is_present()
                 and (slot := candidate.slot()) is not None}, False)

    def reconcile_action(self, action, *, old_context=None, old_slot=None):
        context = self.coordinator.context(action)
        action._scan_context = context
        session = self.coordinator.session_for(context)
        slot = action.slot()
        if slot is None and old_slot is None:
            return
        affected = set() if slot is None else {slot}
        if old_context == context and old_slot is not None:
            affected.add(old_slot)
        filters, authoritative = self._configured_filters(context)
        if slot is not None:
            filters[slot] = action.color_filter()
        was_scanning = session.snapshot().status == 'scanning'
        reconcile_all = authoritative and slot is not None
        if session.reconcile(filters, affected=None if reconcile_all else affected):
            if was_scanning:
                self.coordinator.cancel_context(context)
            self.coordinator.persist_context(context, session)
            self.redraw(context)

    def redraw(self, context):
        for action in self.coordinator._attached_actions():
            try:
                if (getattr(action, 'on_ready_called', False) and action.get_is_present()
                        and self.coordinator.context(action) == context):
                    action.render()
            except Exception:
                log.exception('Unable to render scan action')
                self.coordinator.show_action_error(action)
