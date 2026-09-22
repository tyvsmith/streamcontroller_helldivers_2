"""Own scan sessions by deck, page, and group, and their saved state."""
from pathlib import Path

from loguru import logger as log

from .scan_artwork import catalog_colors
from .scan_session import ScanSession
from .scan_state import ScanStateStore


def scan_group(settings):
    return str(settings.get('group', 'default')).strip() or 'default'


class SessionRegistry:
    """Map action contexts to sessions; restore and persist them."""

    def __init__(self, plugin, state_dir=None):
        self.plugin = plugin
        self.sessions = {}
        self.store = ScanStateStore(state_dir) if state_dir is not None else None
        self.state_errors = {}

    def identity(self, action):
        return self.context_identity(self.context(action))

    def context(self, action):
        group = scan_group(action.get_settings())
        return action.deck_controller, action.page.json_path, group

    def session(self, action):
        return self.session_for(self.context(action))

    def session_for(self, context):
        if context not in self.sessions:
            session = ScanSession()
            if self.store:
                try:
                    session = self.store.load(self.context_identity(context), self.plugin.stratagems)
                except (OSError, ValueError) as error:
                    self.state_errors[context] = f'Unable to restore scan state: {error}'
                    log.warning(self.state_errors[context])
            self.sessions[context] = session
        return self.sessions[context]

    def context_identity(self, context):
        deck, page, group = context
        serial = deck.serial_number()
        if not isinstance(serial, str) or not serial:
            raise ValueError('Deck serial number unavailable')
        return dict(deck=serial, page=str(Path(page).absolute()), group=group)

    def persist(self, action, session):
        self.persist_context(self.context(action), session)

    def persist_context(self, context, session):
        if not self.store:
            return
        try:
            self.write_state(self.context_identity(context), session)
            self.state_errors.pop(context, None)
        except Exception as error:
            self.state_errors[context] = f'Unable to save scan state: {error}'
            log.warning(self.state_errors[context])

    def write_state(self, identity, session):
        colors = catalog_colors(self.plugin.PATH, self.plugin.stratagems)
        lm = getattr(self.plugin, 'lm', None)
        names = {key: lm.get(f'actions.{key}.name') for key in self.plugin.stratagems} if lm else {}
        self.store.save(identity, session, self.plugin.stratagems, colors, names)
