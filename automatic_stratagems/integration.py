"""Optional StreamController registration, settings, and lifecycle boundary."""

import os
from threading import Thread
from weakref import ref

from gi.repository import Adw, Gio, GLib, Gtk
from loguru import logger as log
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.InputBases import KeyAction

from .runtime_install import (
    FEATURE_SETTING, ensure_scanner_runtime, read_status,
)


ACTION_SPECS = {
    'scanner': ("ScanStratagems", "Automatic Stratagem Scanner",
                "automatic_stratagems/assets/icons/scan-update.png"),
    'slot': ("AutomaticStratagem", "Automatic Stratagem",
             "automatic_stratagems/assets/icons/auto-any.png"),
    'back': ("TemporaryScanBack", "Temporary Scan Back",
             "assets/icons/_stepbakcward.png"),
    'page': ("AutoStratagems", "Automatic Stratagem Page",
             "automatic_stratagems/assets/icons/scan-new-page.png"),
}


class UnavailableAutomaticAction(KeyAction):
    """Retain saved action IDs when automatic scanning cannot load."""

    def on_ready(self):
        self.show_error(duration=-1)

    def on_key_down(self, data=None):
        self.show_error(duration=3)

    def on_key_up(self, data=None):
        pass

    def on_key_short_up(self, data=None):
        pass


class UnavailableScanCoordinator:
    def __init__(self, error):
        self.compatibility_error = f"Automatic scanning unavailable: {error}"
        self.temporary_pages = None
        self.closed = False

    @property
    def enabled(self):
        return False

    def settings_changed(self):
        pass

    def screenshot_settings_changed(self):
        pass

    def disable_compatibility(self, message):
        self.compatibility_error = str(message)

    def shutdown(self, timeout=3):
        self.closed = True
        return True


def _register_actions(plugin, registrations):
    for action, (suffix, name, icon) in registrations:
        plugin.add_action_holder(ActionHolder(
            plugin_base=plugin,
            action_base=action,
            action_id=f"net_jslay_helldivers_2::{suffix}",
            action_name=name,
            icon=Gtk.Image.new_from_file(os.path.join(plugin.PATH, icon)),
        ))


class AutomaticIntegration:
    """Own the optional feature's root-plugin integration."""

    def __init__(self, plugin):
        from src.Signals import Signals
        import globals as gl
        from .scan_actions import (
            AutomaticStratagem, AutomaticStratagemPage, ScanCoordinator,
            AutomaticStratagemScanner, TemporaryScanBack,
        )
        from .scan_runner import scan_workers
        from .capture_source import (
            DEFAULT_SCREENSHOT_HOTKEY, SCREENSHOT_TRIGGERS, screenshot_config,
        )
        from .visibility import ChooserCompatibilityError, update_visibility

        self.plugin = plugin
        self.coordinator = ScanCoordinator(
            plugin,
            state_dir=os.path.join(
                gl.DATA_PATH, plugin.get_plugin_id(), "scan-state"),
        )
        self.action_registrations = (
            (AutomaticStratagemScanner, ACTION_SPECS['scanner']),
            (AutomaticStratagem, ACTION_SPECS['slot']),
            (TemporaryScanBack, ACTION_SPECS['back']),
            (AutomaticStratagemPage, ACTION_SPECS['page']),
        )
        self.action_classes = tuple(
            action for action, _spec in self.action_registrations)
        self.scan_workers = scan_workers
        self.screenshot_config = screenshot_config
        self.screenshot_hotkey = DEFAULT_SCREENSHOT_HOTKEY
        self.screenshot_triggers = SCREENSHOT_TRIGGERS
        self.update_visibility = update_visibility
        self.chooser_error = ChooserCompatibilityError
        self.globals = gl
        self.signals = Signals
        self.closed = False
        self.chooser = None
        self.chooser_handler = None
        self.settings_controls = []
        self.enable_row = None
        self.setup_row = None
        self.preparing = False

    def install(self):
        self.plugin.scan_coordinator = self.coordinator
        self_ref = ref(self)

        def page_changed(*args):
            integration = self_ref()
            if integration is not None and not integration.closed:
                return integration.coordinator.page_changed(*args)

        def app_quit(*_args):
            integration = self_ref()
            if integration is not None:
                integration.shutdown()

        self.page_callback = page_changed
        self.quit_callback = app_quit
        try:
            self.globals.signal_manager.connect_signal(
                self.signals.ChangePage, page_changed)
            self.globals.signal_manager.connect_signal(self.signals.AppQuit, app_quit)
        except Exception as error:
            self.coordinator.disable_compatibility(
                f"Automatic scanning lifecycle integration is unsupported: {error}")
        _register_actions(self.plugin, self.action_registrations)
        try:
            self.coordinator.enable_temporary_pages(self.globals.page_manager)
        except Exception as error:
            self.coordinator.disable_compatibility(
                f"Automatic scanning page integration is unsupported: {error}")

    def schedule_visibility(self):
        integration_ref = ref(self)

        def connect_visibility():
            integration = integration_ref()
            if integration is None or integration.closed:
                return False
            return integration.connect_visibility()

        try:
            if self.globals.app is None:
                self.globals.app_loading_finished_tasks.append(connect_visibility)
            else:
                GLib.idle_add(connect_visibility)
        except Exception as error:
            self.disable_compatibility(error)

    def connect_visibility(self):
        if self.closed:
            return False
        try:
            chooser = self.globals.app.main_win.sidebar.action_chooser
            self.update_visibility(chooser, self.coordinator.enabled)
            integration_ref = ref(self)

            def chooser_mapped(mapped):
                integration = integration_ref()
                if integration is None or integration.closed:
                    return
                try:
                    integration.update_visibility(
                        mapped, integration.coordinator.enabled)
                except integration.chooser_error as error:
                    integration.disable_compatibility(error)

            self.chooser = chooser
            self.chooser_handler = chooser.connect("map", chooser_mapped)
        except Exception as error:
            self.disable_compatibility(error)
        return False

    def disable_compatibility(self, error):
        self.coordinator.disable_compatibility(
            "Automatic scanning is unsupported by this StreamController version: "
            f"{error}")
        if self.enable_row is not None:
            self.enable_row.set_subtitle(self.coordinator.compatibility_error)
            self.enable_row.set_sensitive(False)

    def settings_rows(self):
        enable = self._automatic_row()
        self.settings_controls = [
            self._setup_row(), self._workers_row(), self._screenshot_row()]
        for row in self.settings_controls:
            row.set_sensitive(self.coordinator.enabled)
        return [enable, *self.settings_controls]

    def _save_setting(self, key, value):
        settings = self.plugin.get_settings()
        settings[key] = value
        self.plugin.set_settings(settings)

    def _save_screenshot_setting(self, key, value):
        before = self.screenshot_config(self.plugin.get_settings())
        self._save_setting(key, value)
        if self.screenshot_config(self.plugin.get_settings()) != before:
            self.coordinator.screenshot_settings_changed()

    def _automatic_row(self):
        error = self.coordinator.compatibility_error
        row = Adw.SwitchRow(
            title="Enable automatic stratagems",
            subtitle=(error or
                      "Show automatic scan actions in the action chooser and enable "
                      "screenshot scanning. Off by default."),
        )
        row.set_active(self.coordinator.enabled)
        row.set_sensitive(error is None)
        row.connect("notify::active", self._automatic_changed)
        self.enable_row = row
        return row

    def setup_status_text(self):
        """Describe the last recorded scanner preparation for the settings row."""
        record = read_status(self.plugin.PATH)
        if record is None:
            return "The scanner runtime has not been prepared yet."
        state = record.get("state")
        if state == "disabled":
            return ("The scanner runtime is not prepared while automatic "
                    "stratagems are switched off.")
        if state == "error":
            return f"Preparation failed: {record.get('error')}"
        if state in ("ready", "installed"):
            return (f"Ready: {record.get('profile')} runtime verified "
                    f"{record.get('updated_at')}.")
        return "The scanner runtime state is unknown."

    def _setup_row(self):
        row = Adw.ActionRow(title="Scanner setup",
                            subtitle=self.setup_status_text())
        button = Gtk.Button(label="Run setup", valign=Gtk.Align.CENTER)
        button.connect("clicked", self.prepare_runtime)
        row.add_suffix(button)
        self.setup_row = row
        return row

    def prepare_runtime(self, *_arguments):
        """Install the scanner runtime once, off the main thread."""
        if self.closed or self.preparing:
            return
        self.preparing = True
        if self.setup_row is not None:
            self.setup_row.set_subtitle("Preparing the scanner runtime…")
        try:
            Thread(target=self._prepare_runtime, name="hd2-scanner-setup",
                   daemon=True).start()
        except Exception as error:
            self.preparing = False
            log.error(f"Unable to start the scanner runtime setup: {error}")

    def _prepare_runtime(self):
        try:
            ensure_scanner_runtime(self.plugin.PATH, self.plugin.get_settings())
        except Exception as error:
            log.error(f"Unable to prepare the scanner runtime: {error}")
        finally:
            self.preparing = False
        integration_ref = ref(self)

        def refresh():
            integration = integration_ref()
            if integration is not None and not integration.closed:
                integration.refresh_setup_row()
            return False

        try:
            GLib.idle_add(refresh)
        except Exception as error:
            log.error(f"Unable to report the scanner runtime state: {error}")

    def refresh_setup_row(self):
        if self.setup_row is not None:
            self.setup_row.set_subtitle(self.setup_status_text())

    def _automatic_changed(self, row, _property):
        self._save_setting(FEATURE_SETTING, row.get_active())
        self.coordinator.settings_changed()
        for setting_row in self.settings_controls:
            setting_row.set_sensitive(self.coordinator.enabled)
        if self.chooser is not None:
            try:
                self.update_visibility(self.chooser, self.coordinator.enabled)
            except Exception as error:
                self.disable_compatibility(error)
        if row.get_active():
            self.prepare_runtime()

    def _workers_row(self):
        row = Adw.ActionRow(
            title="Scan workers",
            subtitle="Parallel icon matching. Default 2; use 1 to disable parallel matching.",
        )
        adjustment = Gtk.Adjustment(
            value=self.scan_workers(
                self.plugin.get_settings().get("scan_workers", 2)),
            lower=1, upper=32, step_increment=1, page_increment=1,
        )
        spin = Gtk.SpinButton(
            adjustment=adjustment, digits=0, valign=Gtk.Align.CENTER)
        spin.connect(
            "value-changed",
            lambda widget: self._save_setting(
                "scan_workers", widget.get_value_as_int()),
        )
        row.add_suffix(spin)
        return row

    def _screenshot_row(self):
        settings = self.screenshot_config(self.plugin.get_settings())
        section = Adw.ExpanderRow(
            title="Screenshot capture",
            subtitle="Configure the screenshot source used by Automatic and Screenshot scans",
        )
        trigger = Adw.ComboRow(
            title="Trigger", subtitle="Choose how the screenshot is saved")
        trigger.set_model(Gtk.StringList.new(["Hotkey", "Script"]))
        trigger.set_selected(self.screenshot_triggers.index(settings["trigger"]))

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
        for row in (trigger, hotkey, script, folder, delete, help_row):
            section.add_row(row)

        def trigger_changed(row, _property):
            value = self.screenshot_triggers[row.get_selected()]
            hotkey.set_visible(value == "hotkey")
            script.set_visible(value == "script")
            self._save_screenshot_setting("screenshot_trigger", value)

        trigger.connect("notify::selected", trigger_changed)

        def commit_entry(row, key, default=""):
            value = row.get_text().strip() or default
            if row.get_text() != value:
                row.set_text(value)
            self._save_screenshot_setting(key, value)

        for row, key, default in (
                (hotkey, "screenshot_hotkey", self.screenshot_hotkey),
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
            lambda row, _property: self._save_screenshot_setting(
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
        return section

    def shutdown(self, *, uninstall=False):
        self.closed = True
        try:
            if not self.coordinator.shutdown():
                log.error("Automatic scanner cleanup did not finish before shutdown deadline")
        except Exception:
            log.exception("Unable to shut down automatic scanner work")
        temporary_pages = getattr(self.coordinator, "temporary_pages", None)
        if temporary_pages is not None:
            try:
                if uninstall:
                    temporary_pages.cleanup()
                else:
                    temporary_pages.unregister_all()
            except Exception:
                log.exception("Unable to release automatic scanner pages")
        if self.chooser is not None and self.chooser_handler is not None:
            try:
                self.chooser.disconnect(self.chooser_handler)
            except Exception:
                log.exception("Unable to disconnect automatic action visibility")
        self.chooser = None
        self.chooser_handler = None


class UnavailableAutomaticIntegration:
    def __init__(self, plugin, error):
        self.plugin = plugin
        self.coordinator = UnavailableScanCoordinator(error)
        log.error(self.coordinator.compatibility_error)

    def install(self):
        self.plugin.scan_coordinator = self.coordinator
        _register_actions(
            self.plugin,
            tuple((UnavailableAutomaticAction, spec)
                  for spec in ACTION_SPECS.values()))

    def settings_rows(self):
        rows = [
            Adw.SwitchRow(
                title="Enable automatic stratagems",
                subtitle=self.coordinator.compatibility_error,
            ),
            Adw.ActionRow(
                title="Scanner setup",
                subtitle=self.coordinator.compatibility_error,
            ),
            Adw.ActionRow(
                title="Scan workers",
                subtitle=self.coordinator.compatibility_error,
            ),
            Adw.ActionRow(
                title="Screenshot capture",
                subtitle=self.coordinator.compatibility_error,
            ),
        ]
        for row in rows:
            row.set_sensitive(False)
        return rows

    def schedule_visibility(self):
        return False

    def shutdown(self, *, uninstall=False):
        self.coordinator.shutdown()


def create_integration(plugin):
    try:
        return AutomaticIntegration(plugin)
    except Exception as error:
        return UnavailableAutomaticIntegration(plugin, error)
