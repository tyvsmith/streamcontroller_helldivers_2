"""Optional StreamController registration, settings, and lifecycle boundary."""

import os
from threading import Thread
from weakref import ref

from gi.repository import Adw, GLib, Gtk
from loguru import logger as log
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.InputBases import KeyAction

from . import runtime_preparation
from . import settings_rows


# How long to wait for StreamController to rebuild the chooser after a store
# install before treating missing automatic action rows as incompatible.
VISIBILITY_RETRY_MS = 250
VISIBILITY_RETRY_LIMIT = 40

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
        from .visibility import (
            ChooserCompatibilityError, ChooserRowsPending, update_visibility,
        )

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
        self.rows_pending = ChooserRowsPending
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

    def connect_visibility(self, attempt=0):
        if self.closed:
            return False
        try:
            chooser = self.globals.app.main_win.sidebar.action_chooser
            integration_ref = ref(self)
            try:
                self.update_visibility(chooser, self.coordinator.enabled)
            except self.rows_pending as pending:
                # A store install initializes the plugin before StreamController
                # rebuilds the chooser rows, so absent rows are not yet a verdict.
                if attempt >= VISIBILITY_RETRY_LIMIT:
                    raise self.chooser_error(
                        "automatic action rows never appeared in the chooser") from pending

                def retry():
                    integration = integration_ref()
                    if integration is not None and not integration.closed:
                        integration.connect_visibility(attempt + 1)
                    return False

                GLib.timeout_add(VISIBILITY_RETRY_MS, retry)
                return False

            def chooser_mapped(mapped):
                integration = integration_ref()
                if integration is None or integration.closed:
                    return
                try:
                    integration.update_visibility(
                        mapped, integration.coordinator.enabled)
                except integration.rows_pending:
                    return
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
        return settings_rows.build_settings_rows(self)

    def _save_setting(self, key, value):
        settings_rows.save_setting(self.plugin, key, value)

    def _save_screenshot_setting(self, key, value):
        settings_rows.save_screenshot_setting(self, key, value)

    def _automatic_row(self):
        return settings_rows.build_enable_row(self)

    def setup_status_text(self):
        """Describe the last recorded scanner preparation for the settings row."""
        return settings_rows.setup_status_text(self.plugin)

    def _setup_row(self):
        return settings_rows.build_setup_row(self)

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
            runtime_preparation.prepare_scanner_runtime(self.plugin)
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
        settings_rows.automatic_changed(self, row, _property)

    def _workers_row(self):
        return settings_rows.build_workers_row(self)

    def _screenshot_row(self):
        return settings_rows.build_screenshot_row(self)

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
