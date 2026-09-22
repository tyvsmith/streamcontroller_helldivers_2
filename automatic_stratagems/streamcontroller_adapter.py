"""Narrow adapters for the tested StreamController beta.15 object shape."""


class StreamControllerCompatibilityError(RuntimeError):
    pass


def page_topology(action):
    page = getattr(action, 'page', None)
    data = getattr(page, 'dict', None)
    objects = getattr(page, 'action_objects', None)
    if not isinstance(data, dict) or not isinstance(objects, dict):
        return None
    return data, objects


def _topology_index(value):
    if type(value) is int:
        return value if value >= 0 else None
    if (not isinstance(value, str) or not value.isascii()
            or not value.isdecimal()):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def page_action_records(action, action_id):
    """Return sorted matching action records from the tested page topology."""
    topology = page_topology(action)
    if topology is None:
        return None
    data, objects = topology
    keys = data.get('keys')
    object_keys = objects.get('keys')
    if not isinstance(keys, dict) or not isinstance(object_keys, dict):
        return None
    records = []
    for identifier, key in keys.items():
        parts = identifier.split('x', 1) if isinstance(identifier, str) else []
        coordinates = [_topology_index(value) for value in parts]
        if len(coordinates) != 2 or any(value is None for value in coordinates):
            return None
        column, row = coordinates
        if not isinstance(key, dict):
            return None
        states = key.get('states', {})
        if not isinstance(states, dict):
            return None
        for state_key, state_data in states.items():
            state = _topology_index(state_key)
            if state is None or not isinstance(state_data, dict):
                return None
            actions = state_data.get('actions', [])
            if not isinstance(actions, list):
                return None
            for index, action_data in enumerate(actions):
                if not isinstance(action_data, dict):
                    return None
                if action_data.get('id') != action_id:
                    continue
                settings = action_data.get('settings', {})
                if not isinstance(settings, dict):
                    return None
                input_objects = object_keys.get(identifier)
                if input_objects is not None and not isinstance(input_objects, dict):
                    return None
                state_objects = (input_objects.get(state)
                                 if isinstance(input_objects, dict) else None)
                if state_objects is not None and not isinstance(state_objects, dict):
                    return None
                action_object = (state_objects.get(index)
                                 if isinstance(state_objects, dict) else None)
                records.append(((row, column, state, index), settings, action_object))
    return sorted(records, key=lambda record: record[0])


def source_action_address(action):
    input_ident = getattr(action, 'input_ident', None)
    input_type = getattr(input_ident, 'input_type', None)
    identifier = getattr(input_ident, 'json_identifier', None)
    state = getattr(action, 'state', None)
    if (not isinstance(input_type, str) or not input_type
            or not isinstance(identifier, str) or not identifier):
        raise ValueError('Source action address unavailable')
    if type(state) is not int or state < 0:
        raise ValueError('Source action state unavailable')
    index = None
    objects = getattr(getattr(action, 'page', None), 'action_objects', None)
    if isinstance(objects, dict):
        state_actions = objects.get(input_type, {}).get(identifier, {}).get(state, {})
        if isinstance(state_actions, dict):
            index = next((candidate for candidate, value in state_actions.items()
                          if type(candidate) is int and candidate >= 0 and value is action), None)
    if index is None:
        get_index = getattr(action, 'get_own_action_index', None)
        candidate = get_index() if callable(get_index) else None
        if type(candidate) is int and candidate >= 0:
            index = candidate
    if index is None:
        raise ValueError('Source action index unavailable')
    return dict(input_type=input_type, identifier=identifier, state=state, index=index)


def register_page(owner, manager, path):
    if owner is not None and callable(getattr(owner, 'register_page', None)):
        owner.register_page(path)
    else:
        manager.register_page(path)


def unregister_page(owner, manager, path):
    registered = getattr(manager, 'custom_pages', None)
    if registered is None or path in registered:
        manager.unregister_page(path)
    owned = getattr(owner, 'registered_pages', None)
    if owned is not None:
        while path in owned:
            owned.remove(path)


def discard_cached_page(manager, path):
    pages = getattr(manager, 'pages', None)
    if not isinstance(pages, dict):
        raise StreamControllerCompatibilityError(
            'Temporary page cache cleanup is unsupported')
    for cached in pages.values():
        if not isinstance(cached, dict):
            raise StreamControllerCompatibilityError(
                'Temporary page cache cleanup is unsupported')
        entry = cached.get(path)
        if entry:
            page = entry.get('page') if isinstance(entry, dict) else None
            clear = getattr(page, 'clear_action_objects', None)
            if not callable(clear):
                raise StreamControllerCompatibilityError(
                    'Temporary page cache cleanup is unsupported')
            clear()
            cached.pop(path, None)


def wake_media_ticks(controller):
    media_player = getattr(controller, 'media_player', None)
    if not isinstance(getattr(media_player, '_cached_needs_ticks', None), bool):
        return False
    media_player._cached_needs_ticks = True
    wake_set = getattr(getattr(media_player, '_wake_event', None), 'set', None)
    if callable(wake_set):
        wake_set()
    return True


def chooser_action_rows(chooser):
    try:
        expanders = chooser.plugin_group.expander
    except AttributeError as error:
        raise StreamControllerCompatibilityError(
            'Automatic action visibility is unsupported') from error
    rows = []
    for expander in expanders:
        get_rows = getattr(expander, 'get_rows', None)
        if not callable(get_rows):
            raise StreamControllerCompatibilityError(
                'Automatic action visibility is unsupported')
        rows.extend(get_rows())
    return rows
