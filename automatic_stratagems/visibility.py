"""Action chooser integration; retain holders so saved pages still load."""

ACTION_IDS = frozenset(f'net_jslay_helldivers_2::{suffix}' for suffix in
                       ('ScanStratagems', 'AutoStratagems', 'AutomaticStratagem', 'TemporaryScanBack'))


class ChooserCompatibilityError(RuntimeError):
    """The tested StreamController chooser structure is unavailable."""


def update_visibility(chooser, enabled):
    # StreamController has no holder visibility flag. Refresh rows on each map
    # because the chooser can rebuild them after plugin changes.
    try:
        expanders = chooser.plugin_group.expander
    except AttributeError as error:
        raise ChooserCompatibilityError('Automatic action visibility is unsupported') from error
    targets = []
    found = set()
    for expander in expanders:
        get_rows = getattr(expander, 'get_rows', None)
        if not callable(get_rows):
            raise ChooserCompatibilityError('Automatic action visibility is unsupported')
        for row in get_rows():
            holder = getattr(row, 'action_holder', None)
            if holder is not None and holder.action_id in ACTION_IDS:
                if not callable(getattr(row, 'set_visible', None)):
                    raise ChooserCompatibilityError('Automatic action visibility is unsupported')
                targets.append(row)
                found.add(holder.action_id)
    if found != ACTION_IDS:
        raise ChooserCompatibilityError('Automatic action visibility is unsupported')
    for row in targets:
        row.set_visible(enabled)
    return len(targets)
