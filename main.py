import json
import os
from threading import Lock
from weakref import ref

from .stratagem_execution import execute_stratagem

from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.InputBases import KeyAction

from evdev import ecodes, UInput
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from loguru import logger as log

AUTOMATIC_IMPORT_ERROR = None
try:
    from .automatic_stratagems.scan_actions import (
        AutomaticStratagem, AutoStratagems, ScanCoordinator, ScanStratagems, TemporaryScanBack)
    from .automatic_stratagems.scan_runner import scan_workers
    from .automatic_stratagems.visibility import (
        ChooserCompatibilityError, update_visibility)
except Exception as error:
    AUTOMATIC_IMPORT_ERROR = str(error)
    ScanCoordinator = ScanStratagems = AutomaticStratagem = AutoStratagems = TemporaryScanBack = None

    def scan_workers(value=2):
        return 2

from .key_mapping import (
    DEFAULT_DIRECTION_KEY_LAYOUT,
    get_direction_key as resolve_direction_key,
    normalize_direction_key_layout,
)


log.debug("Init HELLDIVERS 2")


# Default settings
DEFAULT_KEY_DELAY = 0.03
DEFAULT_MODIFIER_KEY = "KEY_LEFTCTRL"
DEFAULT_HOLD_MODIFIER = True  # False = press/release, True = hold during sequence
DEFAULT_SHOW_LABELS = True  # Show text labels on buttons

DIRECTION_KEY_LAYOUT_OPTIONS = {
    "Arrow keys": "arrow_keys",
    "WASD keys": "wasd",
}

# Available modifier keys
MODIFIER_KEYS = {
    "Left Ctrl": "KEY_LEFTCTRL",
    "Right Ctrl": "KEY_RIGHTCTRL",
    "Left Alt": "KEY_LEFTALT",
    "Right Alt": "KEY_RIGHTALT",
    "Left Shift": "KEY_LEFTSHIFT",
    "Right Shift": "KEY_RIGHTSHIFT",
}


class StratagemHeroButton(KeyAction):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def on_ready(self):
        self.show()

    def on_key_down(self, data=None):
        self.plugin_base.hero_mode = not self.plugin_base.hero_mode
        self.show()

    def on_key_up(self, data=None):
        pass

    def on_key_short_up(self, data=None):
        pass

    def show(self):
        if self.plugin_base.get_show_labels():
            self.set_top_label(self.plugin_base.lm.get("actions.StratagemHeroToggle.labels.top", ""))
            self.set_center_label(self.plugin_base.lm.get("actions.StratagemHeroToggle.labels.center", "Stratagem"))
            self.set_bottom_label(self.plugin_base.lm.get("actions.StratagemHeroToggle.labels.bottom", "Hero"))
        else:
            self.set_top_label("")
            self.set_center_label("")
            self.set_bottom_label("")

        fname = "hero_off.png"
        if self.plugin_base.hero_mode:
            fname = "hero_on.png"
        self.set_media(
            media_path=os.path.join(self.plugin_base.PATH, "assets", "icons", fname)
        )


# Direction key mappings for the sequence editor
DIRECTION_KEYS = ["UP", "DOWN", "LEFT", "RIGHT"]
DIRECTION_ARROWS = {"UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→"}


class CustomStratagemButton(KeyAction):
    """
    A custom stratagem button that allows users to define their own key sequences.
    Useful for stratagems not yet added to the plugin's mapping.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True

    def on_ready(self):
        self.show()

    def get_sequence(self) -> list:
        """Get the user-defined key sequence."""
        settings = self.get_settings()
        return settings.get("sequence", [])
    
    def get_custom_name(self) -> str:
        """Get the user-defined name for this stratagem."""
        settings = self.get_settings()
        return settings.get("name", "Custom")
    
    def get_custom_labels(self) -> dict:
        """Get the user-defined labels for this stratagem."""
        settings = self.get_settings()
        return settings.get("labels", {"top": "", "center": "", "bottom": "Custom"})

    def show(self):
        if self.plugin_base.get_show_labels():
            labels = self.get_custom_labels()
            self.set_top_label(labels.get("top", ""))
            self.set_center_label(labels.get("center", ""))
            self.set_bottom_label(labels.get("bottom", "Custom"))
        else:
            self.set_top_label("")
            self.set_center_label("")
            self.set_bottom_label("")
        
        # Use custom icon if set, otherwise use default
        settings = self.get_settings()
        custom_icon = settings.get("icon_path")
        if custom_icon and os.path.exists(custom_icon):
            self.set_media(media_path=custom_icon)
        else:
            self.set_media(media_path=os.path.join(self.plugin_base.PATH, "assets", "icons", "custom.png"))

    def get_config_rows(self) -> list:
        """Build the configuration UI for the custom stratagem."""
        rows = []
        
        # Name entry row
        self.name_row = Adw.EntryRow(
            title=self.plugin_base.lm.get("actions.CustomStratagem.config.name", "Stratagem Name")
        )
        self.name_row.set_text(self.get_custom_name())
        self.name_row.connect("changed", self._on_name_changed)
        rows.append(self.name_row)
        
        # Labels configuration
        labels = self.get_custom_labels()
        
        self.top_label_row = Adw.EntryRow(
            title=self.plugin_base.lm.get("actions.CustomStratagem.config.top_label", "Top Label")
        )
        self.top_label_row.set_text(labels.get("top", ""))
        self.top_label_row.connect("changed", self._on_labels_changed)
        rows.append(self.top_label_row)
        
        self.center_label_row = Adw.EntryRow(
            title=self.plugin_base.lm.get("actions.CustomStratagem.config.center_label", "Center Label")
        )
        self.center_label_row.set_text(labels.get("center", ""))
        self.center_label_row.connect("changed", self._on_labels_changed)
        rows.append(self.center_label_row)
        
        self.bottom_label_row = Adw.EntryRow(
            title=self.plugin_base.lm.get("actions.CustomStratagem.config.bottom_label", "Bottom Label")
        )
        self.bottom_label_row.set_text(labels.get("bottom", "Custom"))
        self.bottom_label_row.connect("changed", self._on_labels_changed)
        rows.append(self.bottom_label_row)
        
        # Sequence editor row
        sequence_row = SequenceEditorRow(self)
        rows.append(sequence_row)
        
        return rows
    
    def _on_name_changed(self, entry):
        """Handle name change."""
        settings = self.get_settings()
        settings["name"] = entry.get_text()
        self.set_settings(settings)
    
    def _on_labels_changed(self, entry):
        """Handle label change."""
        settings = self.get_settings()
        settings["labels"] = {
            "top": self.top_label_row.get_text(),
            "center": self.center_label_row.get_text(),
            "bottom": self.bottom_label_row.get_text()
        }
        self.set_settings(settings)
        self.show()

    def on_key_down(self, data=None):
        if self.plugin_base.ui is None:
            log.error("UInput not initialized! Check /dev/uinput permissions.")
            log.error("Try: sudo usermod -aG input $USER (then logout/login)")
            return
        sequence = self.get_sequence()
        name = self.get_custom_name()
        if not sequence:
            log.warning(f"No sequence configured for custom stratagem '{name}'!")
            return
        if self.plugin_base.executing or self.plugin_base.input_lock.locked():
            log.debug("Currently executing other stratagem! Aborting!")
            return
        execute_stratagem(self.plugin_base, name, sequence)

    def on_key_up(self, data=None):
        pass

    def on_key_short_up(self, data=None):
        pass


class SequenceEditorRow(Adw.PreferencesRow):
    """A custom row for editing the stratagem key sequence."""
    
    def __init__(self, action: CustomStratagemButton):
        super().__init__(title="Key Sequence")
        self.action = action
        self.build_ui()
        self.update_sequence_display()
    
    def build_ui(self):
        """Build the sequence editor UI."""
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)
        self.set_child(main_box)
        
        # Header with title
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_bottom=8)
        main_box.append(header_box)
        
        title_label = Gtk.Label(
            label=self.action.plugin_base.lm.get("actions.CustomStratagem.config.sequence", "Key Sequence"),
            xalign=0,
            hexpand=True,
            css_classes=["title"]
        )
        header_box.append(title_label)
        
        # Clear button
        clear_button = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Clear sequence")
        clear_button.connect("clicked", self._on_clear)
        header_box.append(clear_button)
        
        # Current sequence display
        sequence_frame = Gtk.Frame(margin_bottom=8)
        main_box.append(sequence_frame)
        
        self.sequence_display = Gtk.Label(
            label="(empty)",
            margin_start=8,
            margin_end=8,
            margin_top=8,
            margin_bottom=8,
            wrap=True,
            css_classes=["monospace"]
        )
        sequence_frame.set_child(self.sequence_display)
        
        # Direction buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        main_box.append(button_box)
        
        for direction in DIRECTION_KEYS:
            btn = Gtk.Button(label=DIRECTION_ARROWS[direction], tooltip_text=f"Add {direction}")
            btn.set_size_request(48, 48)
            btn.connect("clicked", self._on_direction_clicked, direction)
            button_box.append(btn)
        
        # Backspace button to remove last key
        backspace_btn = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Remove last key")
        backspace_btn.set_size_request(48, 48)
        backspace_btn.connect("clicked", self._on_backspace)
        button_box.append(backspace_btn)
    
    def update_sequence_display(self):
        """Update the sequence display label."""
        sequence = self.action.get_sequence()
        if sequence:
            arrows = [DIRECTION_ARROWS.get(k, k) for k in sequence]
            self.sequence_display.set_text(" ".join(arrows))
        else:
            self.sequence_display.set_text("(empty)")
    
    def _on_direction_clicked(self, button, direction):
        """Handle direction button click."""
        settings = self.action.get_settings()
        sequence = settings.get("sequence", [])
        sequence.append(direction)
        settings["sequence"] = sequence
        self.action.set_settings(settings)
        self.update_sequence_display()
    
    def _on_backspace(self, button):
        """Remove the last key from the sequence."""
        settings = self.action.get_settings()
        sequence = settings.get("sequence", [])
        if sequence:
            sequence.pop()
            settings["sequence"] = sequence
            self.action.set_settings(settings)
            self.update_sequence_display()
    
    def _on_clear(self, button):
        """Clear the entire sequence."""
        settings = self.action.get_settings()
        settings["sequence"] = []
        self.action.set_settings(settings)
        self.update_sequence_display()


class StratagemButton(KeyAction):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        self.stratagem_key = self.action_id.split("::", 1)[1]
        self.stratagem = self.plugin_base.stratagems.get(self.stratagem_key)
        
        if self.stratagem is None:
            log.error(f"Stratagem '{self.stratagem_key}' not found in stratagems.json!")
            self.stratagem = []  # Empty sequence to prevent crashes

    def show(self):
        if self.plugin_base.get_show_labels():
            self.set_top_label(self.plugin_base.lm.get(f"actions.{self.stratagem_key}.labels.top", ""))
            self.set_center_label(self.plugin_base.lm.get(f"actions.{self.stratagem_key}.labels.center", ""))
            self.set_bottom_label(self.plugin_base.lm.get(f"actions.{self.stratagem_key}.labels.bottom", self.plugin_base.lm.get(f"actions.{self.stratagem_key}.name")))
        else:
            self.set_top_label("")
            self.set_center_label("")
            self.set_bottom_label("")
        self.set_media(
            media_path=os.path.join(self.plugin_base.PATH, "assets", "icons", self.stratagem_key + ".png")
        )


    def on_ready(self):
        self.show()

    def on_key_down(self, data=None):
        if self.plugin_base.ui is None:
            log.error("UInput not initialized! Check /dev/uinput permissions.")
            log.error("Try: sudo usermod -aG input $USER (then logout/login)")
            return
        if not self.stratagem:
            log.error(f"No sequence for stratagem '{self.stratagem_key}'!")
            return
        if self.plugin_base.executing or self.plugin_base.input_lock.locked():
            log.debug("Currently executing other stratagem! Aborting!")
            return
        execute_stratagem(self.plugin_base, self.stratagem_key, self.stratagem)

    def on_key_up(self, data=None):
        pass

    def on_key_short_up(self, data=None):
        pass


class UnavailableAutomaticAction(KeyAction):
    """Retain automatic action IDs when their optional integration cannot load."""

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
        self.compatibility_error = f'Automatic scanning unavailable: {error}'
        self.temporary_pages = None
        self.closed = False

    @property
    def enabled(self):
        return False

    def settings_changed(self):
        pass

    def disable_compatibility(self, message):
        self.compatibility_error = str(message)

    def shutdown(self, timeout=3):
        self.closed = True
        return True


class HellDiversPlugin(PluginBase):
    def __init__(self):
        super().__init__()

        self.init_locale_manager()
        self.lm = self.locale_manager
        
        # Enable plugin settings
        self.has_plugin_settings = True
        
        self.ui = None
        self.init_input()

        self.stratagems = None
        self.init_stratagems()

        self.hero_mode = False

        self.executing = False
        self.input_lock = Lock()
        self._automatic_closed = False
        self._automatic_chooser = None
        self._automatic_chooser_handler = None
        automatic_initialized = False
        if ScanCoordinator is None:
            self.scan_coordinator = UnavailableScanCoordinator(AUTOMATIC_IMPORT_ERROR)
            log.error(self.scan_coordinator.compatibility_error)
        else:
            try:
                from src.Signals import Signals
                import globals as gl
                self.scan_coordinator = ScanCoordinator(self, state_dir=os.path.join(
                    gl.DATA_PATH, self.get_plugin_id(), 'scan-state'))
                automatic_initialized = True
            except Exception as error:
                self.scan_coordinator = UnavailableScanCoordinator(error)
                log.error(self.scan_coordinator.compatibility_error)

        if automatic_initialized:
            self_ref = ref(self)

            def page_changed(*args):
                plugin = self_ref()
                if plugin is not None and not plugin._automatic_closed:
                    return plugin.scan_coordinator.page_changed(*args)

            def app_quit(*args):
                plugin = self_ref()
                if plugin is not None:
                    plugin._teardown_automatic_scanning()

            self._automatic_page_callback = page_changed
            self._automatic_quit_callback = app_quit
            try:
                gl.signal_manager.connect_signal(Signals.ChangePage, page_changed)
                gl.signal_manager.connect_signal(Signals.AppQuit, app_quit)
            except Exception as error:
                self.scan_coordinator.disable_compatibility(
                    f'Automatic scanning lifecycle integration is unsupported: {error}')

        automatic_classes = ((ScanStratagems, AutomaticStratagem, TemporaryScanBack, AutoStratagems)
                             if automatic_initialized else
                             (UnavailableAutomaticAction,) * 4)
        for action, suffix, name, icon in (
                (automatic_classes[0], "ScanStratagems", "Automatic Stratagem Scanner", "automatic_stratagems/assets/icons/scan-update.png"),
                (automatic_classes[1], "AutomaticStratagem", "Automatic Stratagem", "automatic_stratagems/assets/icons/auto-any.png"),
                (automatic_classes[2], "TemporaryScanBack", "Temporary Scan Back", "assets/icons/_stepbakcward.png"),
                (automatic_classes[3], "AutoStratagems", "Automatic Stratagem Page", "automatic_stratagems/assets/icons/scan-new-page.png")):
            self.add_action_holder(ActionHolder(plugin_base=self, action_base=action,
                action_id=f"net_jslay_helldivers_2::{suffix}", action_name=name,
                icon=Gtk.Image.new_from_file(os.path.join(self.PATH, icon))))
        if automatic_initialized:
            try:
                self.scan_coordinator.enable_temporary_pages(gl.page_manager)
            except Exception as error:
                self.scan_coordinator.disable_compatibility(
                    f'Automatic scanning page integration is unsupported: {error}')

        for stratagem in self.stratagems:
            try:
                self.add_action_holder(ActionHolder(
                    plugin_base=self,
                    action_base=StratagemButton,
                    action_id=f"net_jslay_helldivers_2::{stratagem}",
                    action_name=self.lm.get(f"actions.{stratagem}.name")
                ))
            except Exception as e:
                log.error(e)

        self.add_action_holder(ActionHolder(
            plugin_base=self,
            action_base=StratagemHeroButton,
            action_id="net_jslay_helldivers_2::StratagemHeroToggle",
            action_name=self.lm.get("actions.StratagemHeroToggle.name")
        ))

        # Add custom stratagem action
        self.add_action_holder(ActionHolder(
            plugin_base=self,
            action_base=CustomStratagemButton,
            action_id="net_jslay_helldivers_2::CustomStratagem",
            action_name=self.lm.get("actions.CustomStratagem.name", "Custom Stratagem")
        ))

        self.register(
            plugin_name=self.lm.get("plugin.name"),
            github_repo="https://github.com/jslay88/streamcontroller_helldivers_2",
            plugin_version="2.4.0",
            app_version="1.5.0-beta"
        )

        if automatic_initialized:
            try:
                def connect_automatic_visibility():
                    plugin = self_ref()
                    if plugin is None or plugin._automatic_closed:
                        return False
                    return plugin._connect_automatic_action_visibility()

                if gl.app is None:
                    gl.app_loading_finished_tasks.append(connect_automatic_visibility)
                else:
                    GLib.idle_add(connect_automatic_visibility)
            except Exception as error:
                self._disable_automatic_compatibility(error)

    def _connect_automatic_action_visibility(self):
        import globals as gl
        if self._automatic_closed or ScanCoordinator is None:
            return False
        try:
            chooser = gl.app.main_win.sidebar.action_chooser
            update_visibility(chooser, self.scan_coordinator.enabled)
            self_ref = ref(self)

            def chooser_mapped(mapped):
                plugin = self_ref()
                if plugin is None or plugin._automatic_closed:
                    return
                try:
                    update_visibility(mapped, plugin.scan_coordinator.enabled)
                except ChooserCompatibilityError as error:
                    plugin._disable_automatic_compatibility(error)

            self._automatic_chooser = chooser
            self._automatic_chooser_handler = chooser.connect('map', chooser_mapped)
        except Exception as error:
            self._disable_automatic_compatibility(error)
        return False

    def _disable_automatic_compatibility(self, error):
        self.scan_coordinator.disable_compatibility(
            f'Automatic scanning is unsupported by this StreamController version: {error}')
        row = getattr(self, '_automatic_enable_row', None)
        if row is not None:
            row.set_subtitle(self.scan_coordinator.compatibility_error)
            row.set_sensitive(False)

    def _teardown_automatic_scanning(self, *, uninstall=False):
        self._automatic_closed = True
        try:
            if not self.scan_coordinator.shutdown():
                log.error('Automatic scanner cleanup did not finish before shutdown deadline')
        except Exception:
            log.exception('Unable to shut down automatic scanner work')
        temporary_pages = getattr(self.scan_coordinator, 'temporary_pages', None)
        if temporary_pages is not None:
            try:
                if uninstall:
                    temporary_pages.cleanup()
                else:
                    temporary_pages.unregister_all()
            except Exception:
                log.exception('Unable to release automatic scanner pages')
        chooser = self._automatic_chooser
        handler = self._automatic_chooser_handler
        if chooser is not None and handler is not None:
            try:
                chooser.disconnect(handler)
            except Exception:
                log.exception('Unable to disconnect automatic action visibility')
        self._automatic_chooser = None
        self._automatic_chooser_handler = None

    def on_uninstall(self):
        self._teardown_automatic_scanning(uninstall=True)
        super().on_uninstall()

    def init_locale_manager(self):
        self.lm = self.locale_manager
        self.lm.set_to_os_default()

    def init_input(self):
        self.ui = None
        try:
            self.ui = UInput({ecodes.EV_KEY: range(0, 300),
                         ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y]}, name="stream-controller-helldivers-2-plugin")
            log.info("UInput initialized successfully")
        except PermissionError as e:
            log.error(f"Permission denied creating UInput: {e}")
            log.error("Fix: Add user to input group: sudo usermod -aG input $USER")
            log.error("Then logout and login again, or reboot.")
        except Exception as e:
            log.error(f"Failed to create UInput: {e}")

    def init_stratagems(self):
        with open(os.path.join(self.PATH, "assets", "data", "stratagems.json")) as f:
            self.stratagems = json.load(f)

    # ---- Settings Helpers ----
    
    def get_key_delay(self) -> float:
        """Get the delay between key presses in seconds."""
        settings = self.get_settings()
        return settings.get("key_delay", DEFAULT_KEY_DELAY)
    
    def get_modifier_key(self) -> str:
        """Get the modifier key to use for opening stratagem menu."""
        settings = self.get_settings()
        return settings.get("modifier_key", DEFAULT_MODIFIER_KEY)

    def get_direction_key_layout(self) -> str:
        """Get the key layout used for stratagem directions."""
        settings = self.get_settings()
        return normalize_direction_key_layout(
            settings.get("direction_key_layout", DEFAULT_DIRECTION_KEY_LAYOUT)
        )

    def get_direction_key(self, direction: str) -> str:
        """Get the evdev key name mapped to a stratagem direction."""
        return resolve_direction_key(direction, self.get_direction_key_layout())
    
    def get_hold_modifier(self) -> bool:
        """Get whether to hold the modifier key during the sequence."""
        settings = self.get_settings()
        return settings.get("hold_modifier", DEFAULT_HOLD_MODIFIER)
    
    def get_show_labels(self) -> bool:
        """Get whether to show labels on buttons."""
        settings = self.get_settings()
        return settings.get("show_labels", DEFAULT_SHOW_LABELS)
    
    def _save_setting(self, key: str, value):
        """Save a single setting."""
        settings = self.get_settings()
        settings[key] = value
        self.set_settings(settings)
    
    # ---- Settings UI ----
    
    def get_settings_area(self):
        """Build and return the settings UI."""
        group = Adw.PreferencesGroup(
            title="Stratagem Settings",
            description="Configure how stratagems are executed and displayed"
        )
        
        # Execution settings
        group.add(self._create_key_delay_row())
        group.add(self._create_modifier_key_row())
        group.add(self._create_direction_key_layout_row())
        group.add(self._create_hold_modifier_row())
        
        # Display settings
        group.add(self._create_show_labels_row())
        group.add(self._create_automatic_stratagems_row())
        self._automatic_settings_rows = [self._create_capture_backend_row(), self._create_scan_workers_row()]
        for row in self._automatic_settings_rows:
            row.set_sensitive(self.scan_coordinator.enabled)
            group.add(row)
        
        return group

    def _create_automatic_stratagems_row(self):
        error = self.scan_coordinator.compatibility_error
        row = Adw.SwitchRow(title='Enable automatic stratagems',
                            subtitle=error or
                            'Show automatic scan actions in the action chooser and enable screenshot scanning. Off by default.')
        row.set_active(self.scan_coordinator.enabled)
        row.set_sensitive(error is None)
        row.connect('notify::active', self._on_automatic_stratagems_changed)
        self._automatic_enable_row = row
        return row

    def _on_automatic_stratagems_changed(self, row, _):
        self._save_setting('automatic_stratagems_enabled', row.get_active())
        self.scan_coordinator.settings_changed()
        for setting_row in self._automatic_settings_rows:
            setting_row.set_sensitive(self.scan_coordinator.enabled)
        chooser = getattr(self, '_automatic_chooser', None)
        if chooser is not None:
            try:
                update_visibility(chooser, self.scan_coordinator.enabled)
            except Exception as error:
                self._disable_automatic_compatibility(error)

    def _create_scan_workers_row(self):
        row = Adw.ActionRow(title='Scan workers',
                            subtitle='Parallel icon matching. Default 2; use 1 to disable parallel matching.')
        adjustment = Gtk.Adjustment(value=scan_workers(self.get_settings().get('scan_workers', 2)),
                                    lower=1, upper=32, step_increment=1, page_increment=1)
        spin = Gtk.SpinButton(adjustment=adjustment, digits=0, valign=Gtk.Align.CENTER)
        spin.connect('value-changed', lambda widget: self._save_setting('scan_workers', widget.get_value_as_int()))
        row.add_suffix(spin)
        return row
    
    def _create_capture_backend_row(self):
        choices = ("auto", "gamescope", "steam", "desktop", "portal", "x11")
        row = Adw.ComboRow(title="Capture backend",
                          subtitle="Automatic selects a route for your desktop. Portal asks for a window; Steam F12 saves a screenshot.")
        row.set_model(Gtk.StringList.new([
            "Automatic", "Gamescope", "Steam F12", "Hyprland desktop",
            "Portal window", "X11 window"]))
        current = self.get_settings().get("capture_backend", "auto")
        row.set_selected(choices.index(current) if current in choices else 0)
        row.connect("notify::selected", lambda widget, _: self._save_setting(
            "capture_backend", choices[widget.get_selected()]))
        return row

    def _create_key_delay_row(self) -> Adw.ActionRow:
        """Create the key delay slider row."""
        row = Adw.ActionRow(
            title="Key Delay",
            subtitle="Delay between key presses (seconds). Increase if stratagems fail."
        )
        
        adjustment = Gtk.Adjustment(
            value=self.get_key_delay(),
            lower=0.01,
            upper=0.20,
            step_increment=0.01,
            page_increment=0.05
        )
        
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=adjustment,
            digits=2,
            draw_value=True,
            hexpand=True
        )
        scale.set_size_request(200, -1)
        scale.connect("value-changed", self._on_key_delay_changed)
        
        row.add_suffix(scale)
        return row
    
    def _on_key_delay_changed(self, scale):
        """Handle key delay slider change."""
        value = round(scale.get_value(), 2)
        self._save_setting("key_delay", value)
    
    def _create_modifier_key_row(self) -> Adw.ComboRow:
        """Create the modifier key combo row."""
        row = Adw.ComboRow(
            title="Modifier Key",
            subtitle="Key to open stratagem menu (when not in Hero mode)"
        )
        
        # Create string list for combo
        string_list = Gtk.StringList()
        modifier_names = list(MODIFIER_KEYS.keys())
        for name in modifier_names:
            string_list.append(name)
        
        row.set_model(string_list)
        
        # Set current selection
        current_key = self.get_modifier_key()
        for i, name in enumerate(modifier_names):
            if MODIFIER_KEYS[name] == current_key:
                row.set_selected(i)
                break
        
        row.connect("notify::selected", self._on_modifier_key_changed, modifier_names)
        return row
    
    def _on_modifier_key_changed(self, row, _, modifier_names):
        """Handle modifier key combo change."""
        selected = row.get_selected()
        if selected < len(modifier_names):
            name = modifier_names[selected]
            key = MODIFIER_KEYS[name]
            self._save_setting("modifier_key", key)

    def _create_direction_key_layout_row(self) -> Adw.ComboRow:
        """Create the direction key layout combo row."""
        row = Adw.ComboRow(
            title="Direction Keys",
            subtitle="Keys mapped to Up, Down, Left, and Right stratagem inputs"
        )

        string_list = Gtk.StringList()
        layout_names = list(DIRECTION_KEY_LAYOUT_OPTIONS.keys())
        for name in layout_names:
            string_list.append(name)

        row.set_model(string_list)

        current_layout = self.get_direction_key_layout()
        for i, name in enumerate(layout_names):
            if DIRECTION_KEY_LAYOUT_OPTIONS[name] == current_layout:
                row.set_selected(i)
                break

        row.connect(
            "notify::selected",
            self._on_direction_key_layout_changed,
            layout_names,
        )
        return row

    def _on_direction_key_layout_changed(self, row, _, layout_names):
        """Handle direction key layout changes."""
        selected = row.get_selected()
        if selected < len(layout_names):
            name = layout_names[selected]
            self._save_setting(
                "direction_key_layout",
                DIRECTION_KEY_LAYOUT_OPTIONS[name],
            )
    
    def _create_hold_modifier_row(self) -> Adw.SwitchRow:
        """Create the hold modifier switch row."""
        row = Adw.SwitchRow(
            title="Hold Modifier Key",
            subtitle="Hold modifier during entire sequence (off = press and release before sequence)"
        )
        
        row.set_active(self.get_hold_modifier())
        row.connect("notify::active", self._on_hold_modifier_changed)
        return row
    
    def _on_hold_modifier_changed(self, row, _):
        """Handle hold modifier switch change."""
        self._save_setting("hold_modifier", row.get_active())
    
    def _create_show_labels_row(self) -> Adw.SwitchRow:
        """Create the show labels switch row."""
        row = Adw.SwitchRow(
            title="Show Labels",
            subtitle="Display text labels on stratagem buttons"
        )
        
        row.set_active(self.get_show_labels())
        row.connect("notify::active", self._on_show_labels_changed)
        return row
    
    def _on_show_labels_changed(self, row, _):
        """Handle show labels switch change."""
        self._save_setting("show_labels", row.get_active())
        # Refresh all action displays
        self._refresh_all_actions()
    
    def _refresh_all_actions(self):
        """Refresh all action buttons to apply label visibility changes."""
        import globals as gl
        
        if not hasattr(gl, 'deck_manager') or gl.deck_manager is None:
            return
        
        # Iterate through all deck controllers
        for deck_controller in gl.deck_manager.deck_controller:
            if deck_controller.active_page is None:
                continue
            
            # Get all actions on the active page
            for action in deck_controller.active_page.get_all_actions():
                # Check if this action belongs to our plugin
                if hasattr(action, 'plugin_base') and action.plugin_base is self:
                    if hasattr(action, 'show'):
                        try:
                            action.show()
                        except Exception as e:
                            log.debug(f"Failed to refresh action: {e}")
