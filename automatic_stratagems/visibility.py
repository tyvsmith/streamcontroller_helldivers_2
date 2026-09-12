"""Action chooser integration; retain holders so saved pages still load."""

from .streamcontroller_adapter import (
    StreamControllerCompatibilityError, chooser_action_rows)

PUBLIC_ACTION_IDS = frozenset(f'net_jslay_helldivers_2::{suffix}' for suffix in
                              ('ScanStratagems', 'AutoStratagems', 'AutomaticStratagem'))
BACK_ACTION_ID = 'net_jslay_helldivers_2::TemporaryScanBack'
ACTION_IDS = PUBLIC_ACTION_IDS | {BACK_ACTION_ID}


class ChooserCompatibilityError(RuntimeError):
    """The tested StreamController chooser structure is unavailable."""


def update_visibility(chooser, enabled):
    # StreamController has no holder visibility flag. Refresh rows on each map
    # because the chooser can rebuild them after plugin changes.
    targets = []
    found = set()
    try:
        rows = chooser_action_rows(chooser)
    except StreamControllerCompatibilityError as error:
        raise ChooserCompatibilityError(str(error)) from error
    for row in rows:
        holder = getattr(row, 'action_holder', None)
        if holder is not None and holder.action_id in ACTION_IDS:
            if not callable(getattr(row, 'set_visible', None)):
                raise ChooserCompatibilityError('Automatic action visibility is unsupported')
            targets.append((row, holder.action_id))
            found.add(holder.action_id)
    if found != ACTION_IDS:
        raise ChooserCompatibilityError('Automatic action visibility is unsupported')
    for row, action_id in targets:
        row.set_visible(enabled and action_id in PUBLIC_ACTION_IDS)
    return len(targets)
