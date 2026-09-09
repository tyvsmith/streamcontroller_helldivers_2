"""Serialized keyboard execution shared by stratagem actions."""

import logging
from time import sleep

from evdev import ecodes

from .key_mapping import get_direction_key


log = logging.getLogger(__name__)


def execute_stratagem(plugin, key, sequence, *, guard=None) -> bool:
    """Execute a validated sequence; reject concurrent calls and release keys on failure."""
    if not plugin.input_lock.acquire(blocking=False):
        return False

    pressed = []
    success = False
    ui = None
    try:
        # A validated press commits its sequence; later page changes affect the next press.
        if guard is not None and not guard():
            return False
        plugin.executing = True
        ui = plugin.ui
        if ui is None or not isinstance(sequence, (list, tuple)) or not sequence:
            return False

        # Resolve all directions before opening the menu or sending any input.
        layout = plugin.get_direction_key_layout()
        codes = [ecodes.ecodes[get_direction_key(direction, layout)] for direction in tuple(sequence)]
        hero_mode = plugin.hero_mode
        modifier = ecodes.ecodes.get(plugin.get_modifier_key(), ecodes.KEY_LEFTCTRL)
        hold_modifier = plugin.get_hold_modifier()
        delay = plugin.get_key_delay()

        def press(code):
            # A failing write can still have reached the device.
            pressed.append(code)
            ui.write(ecodes.EV_KEY, code, 1)
            ui.syn()
            sleep(delay)

        def release(code):
            ui.write(ecodes.EV_KEY, code, 0)
            ui.syn()
            pressed.remove(code)
            sleep(delay)

        if not hero_mode:
            press(modifier)
            if not hold_modifier:
                release(modifier)
        for code in codes:
            press(code)
            release(code)
        if not hero_mode and hold_modifier:
            release(modifier)
        success = True
    except Exception:
        log.exception('Error executing stratagem %s', key)
    finally:
        try:
            for code in reversed(pressed):
                try:
                    ui.write(ecodes.EV_KEY, code, 0)
                    ui.syn()
                except Exception:
                    success = False
                    log.exception('Error releasing stratagem key %s', code)
        finally:
            plugin.executing = False
            plugin.input_lock.release()
    return success
