"""Adwaita settings-row construction for the optional feature's plugin panel.

Each `build_*` function builds one row (or row group) for
`AutomaticIntegration.settings_rows` and stores any handle the integration
needs later (`enable_row`, `settings_section`, `setup_row`,
`setup_button`) directly on the integration object
passed in. Persistence goes back through the integration's own
`_save_setting`/`_save_screenshot_setting` methods so callers that replace
those methods (tests included) keep intercepting saves.
"""

import os

from gi.repository import Adw, Gio, Gtk

from .provision.runtime_install import FEATURE_SETTING, read_status


SECTION_TITLE = "Automatic stratagem settings"


def build_settings_rows(integration):
    """Build the rows for `AutomaticIntegration.settings_rows`.

    Only the feature switch stays outside the collapsed section, whose
    subtitle carries the scanner setup status so failures show while closed.
    """
    enable = build_enable_row(integration)
    section = Adw.ExpanderRow(title=SECTION_TITLE)
    integration.settings_section = section
    integration.setup_needs_action = False
    for row in (build_setup_row(integration), build_workers_row(integration),
                *build_screenshot_rows(integration)):
        section.add_row(row)
    integration.settings_controls = [section]
    section.set_sensitive(integration.coordinator.enabled)
    return [enable, section]


def build_enable_row(integration):
    error = integration.coordinator.compatibility_error
    row = Adw.SwitchRow(
        title="Enable automatic stratagems",
        subtitle=(error or
                  "Captures images of the game screen, through Gamescope or your "
                  "screenshot hotkey, to recognize equipped stratagems and show "
                  "automatic scan actions. Off by default."),
    )
    row.set_active(integration.coordinator.enabled)
    row.set_sensitive(error is None)
    row.connect("notify::active", integration._automatic_changed)
    integration.enable_row = row
    return row


PREPARING_TEXT = "Preparing the scanner runtime…"
VERIFIED_STATES = ("ready", "installed")


def setup_status_text(plugin):
    """Describe the last recorded scanner preparation for the settings row."""
    return _status_text(read_status(plugin.PATH))


def _status_text(record):
    if record is None:
        return "The scanner runtime has not been prepared yet."
    state = record.get("state")
    if state == "disabled":
        return ("The scanner runtime is not prepared while automatic "
                "stratagems are switched off.")
    if state == "error":
        return f"Preparation failed: {record.get('error')}"
    if state in VERIFIED_STATES:
        return (f"Ready: {record.get('profile')} runtime verified "
                f"{record.get('updated_at')}.")
    return "The scanner runtime state is unknown."


def build_setup_row(integration):
    row = Adw.ActionRow(title="Scanner setup")
    button = Gtk.Button(label="Run setup", valign=Gtk.Align.CENTER)
    button.connect("clicked", integration.prepare_runtime)
    row.add_suffix(button)
    integration.setup_row = row
    integration.setup_button = button
    refresh_setup_row(integration)
    return row


def refresh_setup_row(integration):
    """Show the recorded status, and offer Run setup only when it can help.

    The record is the only check: verifying the runtime runs a child
    preflight, too slow for the GTK thread. A runtime that breaks after a
    verified record is repaired by the next scan, which records the outcome.
    """
    row = integration.setup_row
    if row is None:
        return
    if integration.preparing:
        text = summary = PREPARING_TEXT
        needs_action = False
    else:
        record = read_status(integration.plugin.PATH)
        text = _status_text(record)
        summary = _summary_text(record, text)
        needs_action = (integration.coordinator.enabled and
                        (record is None
                         or record.get("state") not in VERIFIED_STATES))
    row.set_subtitle(text)
    integration.setup_button.set_visible(needs_action)
    section = integration.settings_section
    if section is None:
        return
    section.set_subtitle(summary)
    # Open only on the change to needing action; after that the user decides.
    if needs_action and not integration.setup_needs_action:
        section.set_expanded(True)
    integration.setup_needs_action = needs_action


def _summary_text(record, text):
    """Shorten the status for the collapsed section; a failure's detail stays in the row."""
    if record is not None and record.get("state") == "error":
        return "Scanner preparation failed. See Scanner setup."
    return text


def automatic_changed(integration, row, _property):
    integration._save_setting(FEATURE_SETTING, row.get_active())
    integration.coordinator.settings_changed()
    for setting_row in integration.settings_controls:
        setting_row.set_sensitive(integration.coordinator.enabled)
    integration.refresh_setup_row()
    if integration.chooser is not None:
        try:
            integration.update_visibility(
                integration.chooser, integration.coordinator.enabled)
        except Exception as error:
            integration.disable_compatibility(error)
    if row.get_active():
        integration.prepare_runtime()


def build_workers_row(integration):
    row = Adw.ActionRow(
        title="Scan workers",
        subtitle="Parallel icon matching. Default 2; use 1 to disable parallel matching.",
    )
    adjustment = Gtk.Adjustment(
        value=integration.scan_workers(
            integration.plugin.get_settings().get("scan_workers", 2)),
        lower=1, upper=32, step_increment=1, page_increment=1,
    )
    spin = Gtk.SpinButton(
        adjustment=adjustment, digits=0, valign=Gtk.Align.CENTER)
    spin.connect(
        "value-changed",
        lambda widget: integration._save_setting(
            "scan_workers", widget.get_value_as_int()),
    )
    row.add_suffix(spin)
    return row


def build_screenshot_rows(integration):
    """Build the screenshot rows, led by a heading row that names the group."""
    settings = integration.screenshot_config(integration.plugin.get_settings())
    heading = Adw.ActionRow(
        title="Screenshot capture",
        subtitle="Configure the screenshot source used by Automatic and Screenshot scans",
    )
    trigger = Adw.ComboRow(
        title="Trigger", subtitle="Choose how the screenshot is saved")
    trigger.set_model(Gtk.StringList.new(["Hotkey", "Script"]))
    trigger.set_selected(integration.screenshot_triggers.index(settings["trigger"]))

    hotkey = Adw.EntryRow(title="Screenshot keycode")
    hotkey.set_text(settings["hotkey"])
    hotkey.set_show_apply_button(True)
    hotkey.set_visible(settings["trigger"] == "hotkey")
    script = Adw.EntryRow(title="Absolute screenshot script path")
    script.set_text(settings["script"])
    script.set_show_apply_button(True)
    script.set_visible(settings["trigger"] == "script")
    folder = Adw.EntryRow(title="Screenshot folder (blank: Steam folder)")
    folder.set_text(settings["path"])
    folder.set_show_apply_button(True)
    browse = Gtk.Button(label="Browse…", valign=Gtk.Align.CENTER)
    folder.add_suffix(browse)
    delete = Adw.SwitchRow(
        title="Delete after successful scan",
        subtitle="Remove the new screenshot and its matching Steam thumbnail after recognition",
    )
    delete.set_active(settings["delete_after_scan"])
    help_row = Adw.ActionRow(
        title="Screenshot setup",
        subtitle=("Configure a hotkey or script that saves an image containing only "
                  "the complete game window. The plugin does not check which application "
                  "is focused. For hotkeys, focus the game before scanning. If using HDR, "
                  "Gamescope or Steam in-game screenshots are preferred. Steam in-game "
                  "screenshots require the Steam overlay to be enabled."),
    )

    def trigger_changed(row, _property):
        value = integration.screenshot_triggers[row.get_selected()]
        hotkey.set_visible(value == "hotkey")
        script.set_visible(value == "script")
        integration._save_screenshot_setting("screenshot_trigger", value)

    trigger.connect("notify::selected", trigger_changed)

    def commit_entry(row, key, default=""):
        value = row.get_text().strip() or default
        if row.get_text() != value:
            row.set_text(value)
        integration._save_screenshot_setting(key, value)

    for row, key, default in (
            (hotkey, "screenshot_hotkey", integration.screenshot_hotkey),
            (script, "screenshot_script", ""),
            (folder, "screenshot_folder", "")):
        row.connect(
            "apply", lambda current, key=key, default=default:
            commit_entry(current, key, default))
        row.connect(
            "entry-activated", lambda current, key=key, default=default:
            commit_entry(current, key, default))
    delete.connect(
        "notify::active",
        lambda row, _property: integration._save_screenshot_setting(
            "screenshot_delete", row.get_active()),
    )

    def browse_clicked(*_args):
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Choose screenshot folder")
        current = folder.get_text().strip()
        if current:
            dialog.set_initial_folder(
                Gio.File.new_for_path(os.path.expanduser(current)))

        def chosen(current_dialog, result):
            try:
                choice = current_dialog.select_folder_finish(result)
                path = choice.get_path()
            except Exception:
                return
            if isinstance(path, str) and path:
                folder.set_text(path)
                commit_entry(folder, "screenshot_folder")

        dialog.select_folder(None, None, chosen)

    browse.connect("clicked", browse_clicked)
    return [heading, trigger, hotkey, script, folder, delete, help_row]


def save_setting(plugin, key, value):
    settings = plugin.get_settings()
    settings[key] = value
    plugin.set_settings(settings)


def save_screenshot_setting(integration, key, value):
    before = integration.screenshot_config(integration.plugin.get_settings())
    integration._save_setting(key, value)
    if integration.screenshot_config(integration.plugin.get_settings()) != before:
        integration.coordinator.screenshot_settings_changed()
