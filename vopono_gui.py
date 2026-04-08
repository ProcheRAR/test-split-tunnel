#!/usr/bin/env python3
"""
Vopono GUI — графическая обёртка для vopono (custom WireGuard configs).
Зависимости: python-gobject (GTK3), vopono.
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango, Gio, GdkPixbuf  # noqa: E402

# Optional tray support
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3
    HAS_TRAY = True
except (ValueError, ImportError):
    HAS_TRAY = False

import configparser
import os
import sys
import signal
import shutil
import stat
import subprocess
import re
import shlex
import tempfile
import fcntl
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_TITLE = "Vopono GUI"
CONFIG_DIR = Path.home() / ".config" / "vopono-gui"
LAST_CONFIG_FILE = CONFIG_DIR / "last_config"
LAST_CUSTOM_CMD = CONFIG_DIR / "last_custom_cmd"
LAST_DNS_FILE = CONFIG_DIR / "last_dns"
SELECTED_APPS_FILE = CONFIG_DIR / "selected_apps"

DESKTOP_DIRS = [
    Path("/usr/share/applications"),
    Path.home() / ".local" / "share" / "applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local" / "share" / "flatpak" / "exports" / "share" / "applications",
]

# Graphical askpass programs (checked in order)
ASKPASS_CANDIDATES = ["ksshaskpass", "ssh-askpass", "lxqt-openssh-askpass", "x11-ssh-askpass"]
# Fallback dialog tools for building our own askpass
DIALOG_CANDIDATES = ["zenity", "kdialog"]

ICON_SIZE = 48

CSS_STR = """
/* Minimal overrides - system GTK theme handles the rest */

.app-item {
    border-radius: 10px;
    padding: 8px;
    border: 2px solid transparent;
    min-width: 80px;
}
.app-item-selected {
    background-color: alpha(@theme_selected_bg_color, 0.18);
    border: 2px solid @theme_selected_bg_color;
    border-radius: 10px;
    padding: 8px;
    min-width: 80px;
}
.app-name {
    font-size: 10px;
}
.app-name-selected {
    color: @theme_selected_bg_color;
    font-size: 10px;
    font-weight: 700;
}

.log-view {
    font-family: "JetBrains Mono", "Fira Code", "Source Code Pro", monospace;
    font-size: 11px;
    padding: 8px;
}

.section-card {
    padding: 12px;
}
.section-title {
    font-size: 13px;
    font-weight: 700;
}
.selected-apps-label {
    font-weight: 600;
    font-size: 12px;
}
.config-path {
    font-family: "JetBrains Mono", "Fira Code", monospace;
    font-size: 12px;
}
.config-none {
    font-style: italic;
    opacity: 0.6;
}
"""


# ---------------------------------------------------------------------------
# Desktop App Discovery
# ---------------------------------------------------------------------------
class DesktopApp:
    __slots__ = ("name", "exec_cmd", "icon_name", "comment", "categories")

    def __init__(self, name: str, exec_cmd: str, icon_name: str, comment: str = "", categories: str = ""):
        self.name = name
        self.exec_cmd = exec_cmd
        self.icon_name = icon_name
        self.comment = comment
        self.categories = categories

    @property
    def clean_exec(self) -> str:
        """Return exec command without %f %u %F %U etc."""
        cmd = self.exec_cmd
        for token in ("%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k"):
            cmd = cmd.replace(token, "")
        return cmd.strip()


def discover_apps() -> list[DesktopApp]:
    """Parse .desktop files and return list of GUI apps."""
    apps: dict[str, DesktopApp] = {}

    for d in DESKTOP_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*.desktop"):
            try:
                cp = configparser.ConfigParser(interpolation=None)
                cp.read(str(f), encoding="utf-8")
                de = cp["Desktop Entry"]

                # Skip non-application, hidden, or NoDisplay entries
                if de.get("Type", "") != "Application":
                    continue
                if de.get("NoDisplay", "").lower() == "true":
                    continue
                if de.get("Hidden", "").lower() == "true":
                    continue
                # Skip terminal-only apps
                if de.get("Terminal", "").lower() == "true":
                    continue

                name = de.get("Name", "")
                exec_cmd = de.get("Exec", "")
                icon = de.get("Icon", "application-x-executable")
                comment = de.get("Comment", "")
                categories = de.get("Categories", "")

                if name and exec_cmd:
                    key = name.lower()
                    if key not in apps:
                        apps[key] = DesktopApp(name, exec_cmd, icon, comment, categories)
            except Exception:
                continue

    result = sorted(apps.values(), key=lambda a: a.name.lower())
    return result


# ---------------------------------------------------------------------------
# Sudo Wrapper
# ---------------------------------------------------------------------------
class SudoWrapper:
    """Intercepts sudo calls to inject -A flag + graphical askpass.

    Strategy: create a temp dir with a 'sudo' script that calls the REAL
    /usr/bin/sudo but adds the -A flag (use SUDO_ASKPASS for password).
    This preserves all original sudo flags (-E, etc.) while enabling
    graphical password prompts.
    """

    def __init__(self):
        self._tmpdir: str | None = None
        self._askpass_path: str | None = None
        self._real_sudo: str = shutil.which("sudo") or "/usr/bin/sudo"
        self._find_or_create_askpass()

    def _find_or_create_askpass(self):
        # 1. Try native askpass programs
        for candidate in ASKPASS_CANDIDATES:
            path = shutil.which(candidate)
            if path:
                self._askpass_path = path
                return

        # 2. Build askpass from zenity/kdialog
        for tool in DIALOG_CANDIDATES:
            path = shutil.which(tool)
            if path:
                self._askpass_path = self._create_askpass_script(tool, path)
                return

    def _create_askpass_script(self, tool: str, tool_path: str) -> str:
        """Create a small askpass script using zenity or kdialog."""
        d = tempfile.mkdtemp(prefix="vopono_gui_askpass_")
        script = Path(d) / "vopono-askpass"
        if tool == "zenity":
            script.write_text(
                '#!/bin/bash\n'
                f'exec {tool_path} --password --title="Vopono GUI" 2>/dev/null\n'
            )
        elif tool == "kdialog":
            script.write_text(
                '#!/bin/bash\n'
                f'exec {tool_path} --password "Password required for vopono:" 2>/dev/null\n'
            )
        script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        self._tmpdir = d  # will be cleaned up
        return str(script)

    @property
    def available(self) -> bool:
        return self._askpass_path is not None

    @property
    def askpass_name(self) -> str | None:
        return self._askpass_path

    def create(self) -> str:
        """Create temp dir with a 'sudo' wrapper that injects -A flag."""
        if self._tmpdir is None:
            self._tmpdir = tempfile.mkdtemp(prefix="vopono_gui_sudo_")

        sudo_script = Path(self._tmpdir) / "sudo"
        # Wrapper calls the REAL sudo with -A added, preserving all args
        sudo_script.write_text(
            '#!/bin/bash\n'
            '# vopono-gui: wrapper that adds -A for graphical askpass\n'
            f'exec {self._real_sudo} -A "$@"\n'
        )
        sudo_script.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        return self._tmpdir

    def cleanup(self):
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def make_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._tmpdir:
            env["PATH"] = self._tmpdir + ":" + env.get("PATH", "")
        if self._askpass_path:
            env["SUDO_ASKPASS"] = self._askpass_path
        return env


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _save(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n")


def _load(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return default


def _set_nonblocking(fd: int):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


# ---------------------------------------------------------------------------
# App Icon Widget
# ---------------------------------------------------------------------------
class AppIcon(Gtk.EventBox):
    """Clickable app icon with label."""

    def __init__(self, app: DesktopApp, on_toggle):
        super().__init__()
        self.app = app
        self._selected = False
        self._on_toggle = on_toggle

        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._box.set_halign(Gtk.Align.CENTER)
        self._box.set_valign(Gtk.Align.CENTER)
        self._box.get_style_context().add_class("app-item")

        # Icon
        self._image = Gtk.Image()
        self._load_icon(app.icon_name)
        self._box.pack_start(self._image, False, False, 0)

        # Label
        self._label = Gtk.Label(label=app.name)
        self._label.set_max_width_chars(12)
        self._label.set_ellipsize(Pango.EllipsizeMode.END)
        self._label.set_justify(Gtk.Justification.CENTER)
        self._label.set_line_wrap(True)
        self._label.set_lines(2)
        self._label.get_style_context().add_class("app-name")
        self._box.pack_start(self._label, False, False, 0)

        self.add(self._box)
        self.set_tooltip_text(app.comment or app.clean_exec)
        self.connect("button-press-event", self._on_click)

    def _load_icon(self, icon_name: str):
        theme = Gtk.IconTheme.get_default()
        # Try as icon name first
        if theme.has_icon(icon_name):
            pixbuf = theme.load_icon(icon_name, ICON_SIZE, Gtk.IconLookupFlags.FORCE_SIZE)
            self._image.set_from_pixbuf(pixbuf)
            return
        # Try as file path
        if os.path.isfile(icon_name):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(icon_name, ICON_SIZE, ICON_SIZE)
                self._image.set_from_pixbuf(pixbuf)
                return
            except Exception:
                pass
        # Fallback
        if theme.has_icon("application-x-executable"):
            pixbuf = theme.load_icon("application-x-executable", ICON_SIZE, Gtk.IconLookupFlags.FORCE_SIZE)
            self._image.set_from_pixbuf(pixbuf)
        else:
            self._image.set_from_icon_name("application-x-executable", Gtk.IconSize.DIALOG)

    def _on_click(self, _w, _evt):
        self.set_selected(not self._selected)
        self._on_toggle(self)

    @property
    def selected(self):
        return self._selected

    def set_selected(self, val: bool):
        self._selected = val
        ctx = self._box.get_style_context()
        lctx = self._label.get_style_context()
        if val:
            ctx.remove_class("app-item")
            ctx.add_class("app-item-selected")
            lctx.remove_class("app-name")
            lctx.add_class("app-name-selected")
        else:
            ctx.remove_class("app-item-selected")
            ctx.add_class("app-item")
            lctx.remove_class("app-name-selected")
            lctx.add_class("app-name")


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class VoponoWindow(Gtk.ApplicationWindow):

    class State:
        DISCONNECTED = "disconnected"
        CONNECTING = "connecting"
        CONNECTED = "connected"
        ERROR = "error"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_default_size(780, 720)
        self.set_title(APP_TITLE)

        self._process: subprocess.Popen | None = None
        self._pgid: int | None = None
        self._state = self.State.DISCONNECTED
        self._config_path: str | None = _load(LAST_CONFIG_FILE) or None
        self._io_source_out: int | None = None
        self._io_source_err: int | None = None
        self._kill_timeout_id: int | None = None
        self._tmp_config: str | None = None

        self._sudo_wrapper = SudoWrapper()
        self._all_apps = discover_apps()
        self._app_icons: list[AppIcon] = []
        self._selected_apps: list[DesktopApp] = []

        self._apply_css()
        self._build_ui()
        self._restore_selected_apps()
        self._update_state_ui()
        self.show_all()

        self.connect("delete-event", self._on_close)

    # ---- CSS ----

    def _apply_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS_STR.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # ---- Build UI ----

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        # ---- Scrollable content ----
        content_scroll = Gtk.ScrolledWindow()
        content_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content_scroll.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(14)
        content.set_margin_bottom(14)

        # ---- Config section ----
        config_card = self._make_card("КОНФИГУРАЦИЯ WIREGUARD")

        config_row = Gtk.Box(spacing=10)
        config_row.set_margin_top(8)

        self._config_label = Gtk.Label()
        self._config_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._config_label.set_xalign(0)
        self._update_config_label()
        config_row.pack_start(self._config_label, True, True, 0)

        choose_btn = Gtk.Button(label="Выбрать…")
        choose_btn.connect("clicked", self._on_choose_config)
        config_row.pack_end(choose_btn, False, False, 0)

        config_card.pack_start(config_row, False, False, 0)

        # DNS
        dns_row = Gtk.Box(spacing=10)
        dns_row.set_margin_top(6)
        dns_lbl = Gtk.Label(label="DNS")
        dns_row.pack_start(dns_lbl, False, False, 0)
        self._dns_entry = Gtk.Entry()
        self._dns_entry.set_placeholder_text("по умолчанию из конфига")
        self._dns_entry.set_text(_load(LAST_DNS_FILE))
        dns_row.pack_start(self._dns_entry, True, True, 0)
        config_card.pack_start(dns_row, False, False, 0)

        content.pack_start(config_card, False, False, 0)

        # ---- Apps section ----
        apps_card = self._make_card("ПРИЛОЖЕНИЯ")

        # Search + selected count
        search_row = Gtk.Box(spacing=10)
        search_row.set_margin_top(6)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Поиск…")

        self._search_entry.connect("search-changed", self._on_search)
        search_row.pack_start(self._search_entry, True, True, 0)

        self._selected_label = Gtk.Label(label="")
        self._selected_label.get_style_context().add_class("selected-apps-label")
        search_row.pack_end(self._selected_label, False, False, 0)

        apps_card.pack_start(search_row, False, False, 0)

        # App grid
        grid_scroll = Gtk.ScrolledWindow()
        grid_scroll.set_min_content_height(200)
        grid_scroll.set_max_content_height(280)
        grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        grid_scroll.set_margin_top(8)

        self._app_flow = Gtk.FlowBox()
        self._app_flow.set_valign(Gtk.Align.START)
        self._app_flow.set_max_children_per_line(30)
        self._app_flow.set_min_children_per_line(4)
        self._app_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._app_flow.set_homogeneous(True)

        for app in self._all_apps:
            icon = AppIcon(app, self._on_app_toggled)
            self._app_icons.append(icon)
            self._app_flow.add(icon)

        grid_scroll.add(self._app_flow)
        apps_card.pack_start(grid_scroll, True, True, 0)

        # Custom command entry
        cmd_row = Gtk.Box(spacing=10)
        cmd_row.set_margin_top(8)
        cmd_lbl = Gtk.Label(label="Свой путь / команда")
        cmd_row.pack_start(cmd_lbl, False, False, 0)

        self._custom_cmd_entry = Gtk.Entry()
        self._custom_cmd_entry.set_placeholder_text("/usr/bin/myapp или команда")
        self._custom_cmd_entry.set_text(_load(LAST_CUSTOM_CMD))
        cmd_row.pack_start(self._custom_cmd_entry, True, True, 0)

        apps_card.pack_start(cmd_row, False, False, 0)

        content.pack_start(apps_card, True, True, 0)

        # ---- Action buttons ----
        btn_row = Gtk.Box(spacing=14)
        btn_row.set_halign(Gtk.Align.CENTER)
        btn_row.set_margin_top(4)

        self._connect_btn = Gtk.Button(label="Подключиться")
        self._connect_btn.set_size_request(200, -1)
        self._connect_btn.get_style_context().add_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
        self._connect_btn.connect("clicked", self._on_connect)
        btn_row.pack_start(self._connect_btn, False, False, 0)

        self._disconnect_btn = Gtk.Button(label="Остановить")
        self._disconnect_btn.set_size_request(200, -1)
        self._disconnect_btn.get_style_context().add_class(Gtk.STYLE_CLASS_DESTRUCTIVE_ACTION)
        self._disconnect_btn.connect("clicked", self._on_disconnect)
        btn_row.pack_start(self._disconnect_btn, False, False, 0)

        self._clean_btn = Gtk.Button(label="🧹 Очистка")
        self._clean_btn.set_tooltip_text("Аварийная очистка (vopono clean) — удалить зависшие namespace")
        self._clean_btn.connect("clicked", self._on_clean)
        btn_row.pack_start(self._clean_btn, False, False, 0)

        content.pack_start(btn_row, False, False, 0)

        # ---- Log section ----
        log_card = self._make_card("ЛОГ")

        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_min_content_height(120)
        log_scroll.set_vexpand(False)
        log_scroll.set_margin_top(6)

        self._log_buffer = Gtk.TextBuffer()
        self._log_view = Gtk.TextView(buffer=self._log_buffer)
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_view.get_style_context().add_class("log-view")
        log_scroll.add(self._log_view)

        log_card.pack_start(log_scroll, True, True, 0)
        content.pack_start(log_card, False, False, 0)

        content_scroll.add(content)
        root.pack_start(content_scroll, True, True, 0)

        # ---- Status bar ----
        self._status_bar = Gtk.Box(spacing=8)
        self._status_bar.set_margin_start(16)
        self._status_bar.set_margin_end(16)
        self._status_bar.set_margin_top(6)
        self._status_bar.set_margin_bottom(10)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0)
        self._status_bar.pack_start(self._status_label, True, True, 0)

        root.pack_end(self._status_bar, False, False, 0)

        # Askpass warning
        if not self._sudo_wrapper.askpass_name:
            self._log("⚠ Не найден графический askpass (pkexec, ksshaskpass, ssh-askpass).\n"
                      "  Установите polkit для графического запроса пароля.\n")

    def _make_card(self, title_text: str) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.get_style_context().add_class("section-card")

        lbl = Gtk.Label(label=title_text)
        lbl.set_xalign(0)
        lbl.get_style_context().add_class("section-title")
        card.pack_start(lbl, False, False, 0)
        return card

    # ---- Config label ----

    def _update_config_label(self):
        if self._config_path:
            self._config_label.set_text(self._config_path)
            ctx = self._config_label.get_style_context()
            ctx.remove_class("config-none")
            ctx.add_class("config-path")
        else:
            self._config_label.set_text("не выбран — нажмите «Выбрать»")
            ctx = self._config_label.get_style_context()
            ctx.remove_class("config-path")
            ctx.add_class("config-none")

    # ---- App grid callbacks ----

    def _on_app_toggled(self, icon: AppIcon):
        if icon.selected:
            if icon.app not in self._selected_apps:
                self._selected_apps.append(icon.app)
        else:
            if icon.app in self._selected_apps:
                self._selected_apps.remove(icon.app)
        self._update_selected_label()

    def _update_selected_label(self):
        n = len(self._selected_apps)
        custom = self._custom_cmd_entry.get_text().strip()
        total = n + (1 if custom else 0)
        if total == 0:
            self._selected_label.set_text("")
        else:
            names = [a.name for a in self._selected_apps]
            if custom:
                names.append(os.path.basename(custom))
            self._selected_label.set_text(f"Выбрано: {', '.join(names)}")

    def _on_search(self, entry):
        query = entry.get_text().lower().strip()
        for icon in self._app_icons:
            visible = query in icon.app.name.lower() or query in icon.app.clean_exec.lower()
            icon.get_parent().set_visible(visible)  # FlowBoxChild

    def _restore_selected_apps(self):
        saved = _load(SELECTED_APPS_FILE)
        if not saved:
            return
        saved_names = set(saved.split("\n"))
        for icon in self._app_icons:
            if icon.app.name in saved_names:
                icon.set_selected(True)
                self._selected_apps.append(icon.app)
        self._update_selected_label()

    def _save_selected_apps(self):
        names = "\n".join(a.name for a in self._selected_apps)
        _save(SELECTED_APPS_FILE, names)

    # ---- Build launch command ----

    def _build_app_command(self) -> str | None:
        """Build the app command string. Returns None if nothing selected."""
        executables: list[str] = []

        for app in self._selected_apps:
            executables.append(app.clean_exec)

        custom = self._custom_cmd_entry.get_text().strip()
        if custom:
            executables.append(custom)

        if not executables:
            return None

        if len(executables) == 1:
            return executables[0]

        # Multiple apps: wrap each in sh -c for safety, then combine
        # This handles complex Exec lines with env vars, spaces, etc.
        parts = [f"sh -c {shlex.quote(e)}" for e in executables]
        inner = " & ".join(parts) + " & wait"
        return f"bash -c {shlex.quote(inner)}"

    # ---- Status UI ----

    def _set_state(self, state: str, detail: str = ""):
        self._state = state
        self._update_state_ui(detail)
        self._update_tray()

    def _update_state_ui(self, detail: str = ""):
        match self._state:
            case self.State.DISCONNECTED:
                self._status_label.set_text("● Отключено")
                self._connect_btn.set_sensitive(True)
                self._disconnect_btn.set_sensitive(False)
            case self.State.CONNECTING:
                self._status_label.set_text("● Подключение…")
                self._connect_btn.set_sensitive(False)
                self._disconnect_btn.set_sensitive(True)
            case self.State.CONNECTED:
                name = detail or (os.path.basename(self._config_path) if self._config_path else "")
                self._status_label.set_text(f"● Подключено — {name}")
                self._connect_btn.set_sensitive(False)
                self._disconnect_btn.set_sensitive(True)
            case self.State.ERROR:
                self._status_label.set_text(f"● Ошибка{': ' + detail if detail else ''}")
                self._connect_btn.set_sensitive(True)
                self._disconnect_btn.set_sensitive(False)

    def _update_tray(self):
        """Update tray icon and menu based on state."""
        if not hasattr(self, '_tray') or self._tray is None:
            return
        if self._state == self.State.CONNECTED:
            self._tray.set_icon_full("network-vpn", "VPN подключён")
        else:
            self._tray.set_icon_full("network-vpn-disconnected", "VPN отключён")
        self._tray.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        # Update menu items
        if hasattr(self, '_tray_app') and self._tray_app:
            self._tray_app._update_tray_menu()

    # ---- Log ----

    def _log(self, text: str):
        end_iter = self._log_buffer.get_end_iter()
        self._log_buffer.insert(end_iter, text)
        GLib.idle_add(self._scroll_log_to_end)

    def _scroll_log_to_end(self):
        end = self._log_buffer.get_end_iter()
        self._log_view.scroll_to_iter(end, 0.0, False, 0, 0)
        return False

    # ---- File chooser ----

    def _on_choose_config(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Выберите WireGuard конфиг",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )

        filt = Gtk.FileFilter()
        filt.set_name("WireGuard конфиги (*.conf)")
        filt.add_pattern("*.conf")
        dialog.add_filter(filt)

        filt_all = Gtk.FileFilter()
        filt_all.set_name("Все файлы")
        filt_all.add_pattern("*")
        dialog.add_filter(filt_all)

        if self._config_path and os.path.isfile(self._config_path):
            dialog.set_filename(self._config_path)
        else:
            dialog.set_current_folder(str(Path.home()))

        if dialog.run() == Gtk.ResponseType.OK:
            self._config_path = dialog.get_filename()
            self._update_config_label()
            _save(LAST_CONFIG_FILE, self._config_path)
        dialog.destroy()

    # ---- Connect / Disconnect ----

    @staticmethod
    def _prepare_config(src_path: str) -> str:
        """Create a temp copy of the WireGuard config with fixes for vopono.

        Fixes applied:
        - Address without CIDR mask: 10.0.0.1 -> 10.0.0.1/32
        - Removes ListenPort (vopono warns about it)
        """
        text = Path(src_path).read_text()
        lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            # Skip ListenPort in [Interface] — vopono doesn't use it
            if stripped.lower().startswith("listenport"):
                continue
            # Fix Address without CIDR
            m = re.match(r'^(\s*Address\s*=\s*)(.+)$', line, re.IGNORECASE)
            if m:
                prefix, addrs = m.group(1), m.group(2)
                fixed_parts = []
                for addr in addrs.split(','):
                    addr = addr.strip()
                    if addr and '/' not in addr:
                        # Add /32 for IPv4, /128 for IPv6
                        if ':' in addr:
                            addr += '/128'
                        else:
                            addr += '/32'
                    fixed_parts.append(addr)
                line = prefix + ', '.join(fixed_parts)
            lines.append(line)

        fd, tmp_path = tempfile.mkstemp(suffix='.conf', prefix='vopono_gui_')
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return tmp_path

    def _on_connect(self, _btn):
        if not self._config_path or not os.path.isfile(self._config_path):
            self._set_state(self.State.ERROR, "конфиг не выбран или не найден")
            return

        app_cmd = self._build_app_command()
        if not app_cmd:
            self._set_state(self.State.ERROR, "не выбрано ни одного приложения")
            return

        # Save state
        self._save_selected_apps()
        _save(LAST_CUSTOM_CMD, self._custom_cmd_entry.get_text().strip())
        _save(LAST_DNS_FILE, self._dns_entry.get_text().strip())

        # Prepare config (fix Address CIDR, remove ListenPort)
        try:
            self._tmp_config = self._prepare_config(self._config_path)
        except Exception as exc:
            self._set_state(self.State.ERROR, f"ошибка чтения конфига: {exc}")
            return

        # Build vopono command
        cmd = ["vopono", "-v", "exec", "--custom", self._tmp_config,
               "--protocol", "wireguard", "--disable-ipv6"]

        dns = self._dns_entry.get_text().strip()
        if dns:
            cmd.extend(["--dns", dns])

        cmd.append(app_cmd)

        self._log_buffer.set_text("")
        self._log(f"▶ {' '.join(cmd)}\n\n")
        self._set_state(self.State.CONNECTING)

        # Create sudo wrapper
        wrapper_dir = self._sudo_wrapper.create()
        env = self._sudo_wrapper.make_env()
        self._log(f"  sudo wrapper: {wrapper_dir}/sudo\n\n")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                preexec_fn=os.setsid,
            )
            self._pgid = os.getpgid(self._process.pid)
        except FileNotFoundError:
            self._set_state(self.State.ERROR, "vopono не найден в PATH")
            self._sudo_wrapper.cleanup()
            return
        except Exception as exc:
            self._set_state(self.State.ERROR, str(exc))
            self._sudo_wrapper.cleanup()
            return

        _set_nonblocking(self._process.stdout.fileno())
        _set_nonblocking(self._process.stderr.fileno())

        self._io_source_out = GLib.io_add_watch(
            self._process.stdout, GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_io_out,
        )
        self._io_source_err = GLib.io_add_watch(
            self._process.stderr, GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_io_err,
        )

        GLib.timeout_add(500, self._poll_process)

    def _read_io(self, fd, condition):
        try:
            data = fd.read(8192)
        except (BlockingIOError, OSError):
            data = None

        if data:
            text = data.decode("utf-8", errors="replace")
            self._log(text)
            if "Created netns" in text or "Namespace exists" in text:
                self._set_state(self.State.CONNECTED)
        return not (condition & GLib.IOCondition.HUP)

    def _on_io_out(self, fd, condition):
        keep = self._read_io(fd, condition)
        if not keep:
            self._io_source_out = None
        return keep

    def _on_io_err(self, fd, condition):
        keep = self._read_io(fd, condition)
        if not keep:
            self._io_source_err = None
        return keep

    def _poll_process(self) -> bool:
        if self._process is None:
            return False
        ret = self._process.poll()
        if ret is not None:
            if self._state == self.State.CONNECTING:
                self._set_state(self.State.ERROR, f"процесс завершился (код {ret})")
            elif self._state != self.State.DISCONNECTED:
                self._log(f"\n■ Процесс завершён (код {ret})\n")
                self._set_state(self.State.DISCONNECTED)
            self._cleanup_process()
            return False
        return True

    def _on_disconnect(self, _btn=None):
        self._stop_process()

    def _on_clean(self, _btn=None):
        """Emergency cleanup: find and remove stale vopono namespaces."""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Аварийная очистка",
        )
        dialog.format_secondary_text(
            "Будут удалены все зависшие network namespaces vopono.\n"
            "Используйте только если предыдущие подключения не завершились корректно."
        )
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        wrapper_dir = self._sudo_wrapper.create()
        env = self._sudo_wrapper.make_env()
        self._log("\n🧹 Очистка namespace…\n")
        try:
            # List all vopono namespaces (prefixed with vo_)
            result = subprocess.run(
                ["ip", "netns", "list"],
                capture_output=True, text=True, timeout=5,
            )
            ns_list = [
                line.split()[0] for line in result.stdout.strip().splitlines()
                if line.strip().startswith("vo_")
            ]

            if not ns_list:
                self._log("Зависших namespace не найдено.\n")
                return

            self._log(f"Найдено namespace: {', '.join(ns_list)}\n")
            for ns in ns_list:
                # Delete veth interface
                veth = f"{ns}_d"
                subprocess.run(
                    ["sudo", "ip", "link", "delete", veth],
                    capture_output=True, text=True, env=env, timeout=10,
                )
                # Delete namespace
                res = subprocess.run(
                    ["sudo", "ip", "netns", "delete", ns],
                    capture_output=True, text=True, env=env, timeout=10,
                )
                if res.returncode == 0:
                    self._log(f"  ✓ {ns} удалён\n")
                else:
                    self._log(f"  ✗ {ns}: {res.stderr.strip()}\n")

            # Clean up lockfiles
            locks_dir = Path.home() / ".config" / "vopono" / "locks"
            if locks_dir.is_dir():
                for d in locks_dir.iterdir():
                    if d.is_dir() and d.name.startswith("vo_"):
                        shutil.rmtree(d, ignore_errors=True)

            # Reload NetworkManager
            subprocess.run(
                ["sudo", "nmcli", "connection", "reload"],
                capture_output=True, env=env, timeout=5,
            )
            self._log("✓ Очистка завершена\n")
        except subprocess.TimeoutExpired:
            self._log("⚠ Таймаут очистки\n")
        except Exception as exc:
            self._log(f"⚠ Ошибка: {exc}\n")
        finally:
            self._sudo_wrapper.cleanup()

    def _stop_process(self):
        if self._process is None:
            return
        self._log("\n⏹ Отключение…\n")
        try:
            if self._pgid:
                os.killpg(self._pgid, signal.SIGTERM)
            else:
                self._process.terminate()
        except ProcessLookupError:
            pass
        self._kill_timeout_id = GLib.timeout_add(3000, self._force_kill)

    def _force_kill(self) -> bool:
        self._kill_timeout_id = None
        if self._process and self._process.poll() is None:
            self._log("⚠ SIGKILL — принудительное завершение\n")
            try:
                if self._pgid:
                    os.killpg(self._pgid, signal.SIGKILL)
                else:
                    self._process.kill()
            except ProcessLookupError:
                pass
        return False

    def _cleanup_process(self):
        for src in (self._io_source_out, self._io_source_err, self._kill_timeout_id):
            if src:
                try:
                    GLib.source_remove(src)
                except Exception:
                    pass
        self._io_source_out = self._io_source_err = self._kill_timeout_id = None
        self._process = None
        self._pgid = None
        self._sudo_wrapper.cleanup()
        # Remove temp config
        if self._tmp_config and os.path.isfile(self._tmp_config):
            try:
                os.unlink(self._tmp_config)
            except OSError:
                pass
            self._tmp_config = None

    # ---- Window close ----

    def _on_close(self, _widget, _event):
        # If VPN is active, ask for confirmation or minimize to tray
        if self._state in (self.State.CONNECTED, self.State.CONNECTING):
            # If tray is available, minimize to tray instead of closing
            if HAS_TRAY and hasattr(self, '_tray') and self._tray:
                self.hide()
                return True

            # No tray — show confirmation dialog
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.YES_NO,
                text="VPN сейчас активен",
            )
            dialog.format_secondary_text(
                "Закрытие программы завершит работу всех запущенных через неё приложений.\n"
                "Вы уверены?"
            )
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.YES:
                return True  # block close

        if self._process and self._process.poll() is None:
            self._stop_process()
            GLib.timeout_add(500, self._check_and_quit)
            return True
        self._sudo_wrapper.cleanup()
        return False

    def _check_and_quit(self) -> bool:
        if self._process and self._process.poll() is None:
            if not hasattr(self, "_quit_attempts"):
                self._quit_attempts = 0
            self._quit_attempts += 1
            if self._quit_attempts < 8:
                return True
            self._force_kill()
        self._cleanup_process()
        self.destroy()
        Gtk.main_quit()
        return False


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class VoponoApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.vopono.gui")
        self._window: VoponoWindow | None = None

    def do_activate(self):
        if not self._window:
            self._window = VoponoWindow(application=self)
            self._setup_tray()
        self._window.present()

    def _setup_tray(self):
        """Create system tray indicator."""
        if not HAS_TRAY:
            return
        indicator = AyatanaAppIndicator3.Indicator.new(
            "vopono-gui",
            "network-vpn-disconnected",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        indicator.set_title("Vopono GUI")

        menu = Gtk.Menu()

        item_show = Gtk.MenuItem(label="Показать окно")
        item_show.connect("activate", lambda _: self._window.present())
        menu.append(item_show)

        menu.append(Gtk.SeparatorMenuItem())

        self._tray_connect = Gtk.MenuItem(label="Подключиться")
        self._tray_connect.connect("activate", lambda _: (
            self._window.present(), self._window._on_connect(None)))
        menu.append(self._tray_connect)

        self._tray_disconnect = Gtk.MenuItem(label="Отключиться")
        self._tray_disconnect.connect("activate", lambda _: self._window._on_disconnect())
        self._tray_disconnect.set_sensitive(False)
        menu.append(self._tray_disconnect)

        menu.append(Gtk.SeparatorMenuItem())

        item_clean = Gtk.MenuItem(label="\U0001f9f9 Аварийная очистка")
        item_clean.connect("activate", lambda _: (
            self._window.present(), self._window._on_clean()))
        menu.append(item_clean)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Выход")
        item_quit.connect("activate", self._on_tray_quit)
        menu.append(item_quit)

        menu.show_all()
        indicator.set_menu(menu)

        # Store reference so window can update it
        self._window._tray = indicator
        self._window._tray_app = self

    def _on_tray_quit(self, _item):
        """Quit from tray — force close."""
        if self._window:
            if self._window._process and self._window._process.poll() is None:
                self._window._stop_process()
                GLib.timeout_add(500, self._force_quit)
                return
            self._window._sudo_wrapper.cleanup()
        self.quit()

    def _force_quit(self) -> bool:
        if self._window and self._window._process and self._window._process.poll() is None:
            self._window._force_kill()
        if self._window:
            self._window._cleanup_process()
        self.quit()
        return False

    def _update_tray_menu(self):
        """Called by VoponoWindow._update_tray."""
        if not hasattr(self, '_tray_connect'):
            return
        connected = self._window and self._window._state in (
            VoponoWindow.State.CONNECTED, VoponoWindow.State.CONNECTING)
        self._tray_connect.set_sensitive(not connected)
        self._tray_disconnect.set_sensitive(connected)


def main():
    def _sig_handler(signum, _frame):
        def _do_quit():
            if app._window:
                app._window._stop_process()
                GLib.timeout_add(600, lambda: app.quit() or False)
            else:
                app.quit()
            return False
        GLib.idle_add(_do_quit)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    app = VoponoApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
