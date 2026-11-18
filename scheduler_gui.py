import json
import os
import subprocess
import sys
import builtins
import threading
import time
import tkinter as tk
import re
import io
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from tkinter import messagebox, ttk
from urllib.parse import urlparse

import customtkinter as ctk  # pyright: ignore[reportMissingImports]
from config import (CONFIG, USER_CONFIG_FILE, save_user_config,
                    runtime_config_ready, export_config_backup, import_config_backup,
                    reload_config_from_disk)

try:
    import pystray  # pyright: ignore[reportMissingImports]
    from PIL import Image, ImageDraw  # pyright: ignore[reportMissingImports]
except Exception:
    pystray = None
    Image = None
    ImageDraw = None

try:
    # Ensure PyInstaller includes worklog_reminder for frozen inline execution.
    import worklog_reminder as _wr_bundle_hint  # noqa: F401
except Exception:
    _wr_bundle_hint = None

# Same two shifts as the CST-NOC roster (worklog_reminder.SHIFT_ORDER).
SHIFT_NAMES = ("morning", "night")


# ── Design Tokens ─────────────────────────────────────────────────────────────
# Corporate light theme, same palette as the reminder email.
BG          = "#f2f4f7"
SURFACE     = "#ffffff"
SURFACE_ALT = "#f9fafb"
ZEBRA       = "#f9fafb"
HEAD_BG     = "#f2f4f7"
BORDER      = "#d0d5dd"
BORDER_LT   = "#eaecf0"

TEXT        = "#1d2939"
TEXT_MUTED  = "#667085"
TEXT_DIM    = "#98a2b3"

NAVY          = "#0b2a4a"
NAVY_HVR      = "#163d66"
NAVY_SOFT     = "#e9eef5"
ON_NAVY       = "#ffffff"
ON_NAVY_MUTED = "#b6c4d6"

ACCENT      = NAVY
ACCENT_HVR  = NAVY_HVR
ON_ACCENT   = ON_NAVY
GOOD        = "#027a48"
GOOD_HVR    = "#05603a"
GOOD_DIM    = "#ecfdf3"
WARN        = "#b54708"
WARN_DIM    = "#fffaeb"
BAD         = "#b42318"
BAD_HVR     = "#912018"
BAD_DIM     = "#fef3f2"

FONT_MONO   = "Consolas"
FONT_UI     = "Segoe UI"
FONT_EMOJI  = "Segoe UI Emoji"
# ─────────────────────────────────────────────────────────────────────────────


class SchedulerGui:
    REFRESH_PROFILES = {"Realtime": 10, "Balanced": 30, "Low API Load": 120}
    TITLE_MAX_CHARS  = 9999
    TITLE_MIN_WIDTH  = 220
    TITLE_MAX_WIDTH  = 520

    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title("CLL MESDP Ticketing Worklog")
        self.root.geometry("1280x860")
        self.root.minsize(1100, 720)
        try:
            self.root.after(250, lambda: self.root.state("zoomed"))
        except tk.TclError:
            pass

        self.project_root        = self._resolve_project_root()
        self.start_script        = os.path.join(self.project_root, "start_scheduler.ps1")
        self.stop_script         = os.path.join(self.project_root, "stop_scheduler.ps1")
        self.restart_script      = os.path.join(self.project_root, "restart_scheduler.ps1")
        self.monitor_script      = os.path.join(self.project_root, "monitor_scheduler.ps1")
        self.python_exe          = os.path.join(self.project_root, ".venv", "Scripts", "python.exe")
        self.shift_settings_file = os.path.join(
            os.path.expandvars("%APPDATA%"), "CLL MESDP", "shift_settings.json"
        )
        self.gui_settings_file   = os.path.join(
            os.path.expandvars("%APPDATA%"), "CLL MESDP", "scheduler_gui_settings.json"
        )

        self.auto_refresh_ms             = 30000
        self.auto_refresh_job            = None
        self.auto_refresh_countdown_job  = None
        self.next_auto_refresh_at: float | None = None
        self.current_scheduler_state     = "UNKNOWN"
        self.settings_window             = None
        self.config_window               = None
        self.latest_tickets: list[dict]  = []
        self.last_data_at: float | None  = None
        self._data_loading               = False
        self._table_resize_job: str | None        = None
        self._title_tooltip: ctk.CTkToplevel | None        = None
        self._title_tooltip_label: ctk.CTkLabel | None     = None
        self._title_hover_item: str | None        = None
        self._full_title_by_item: dict[str, str]  = {}
        self._user_resized_columns = False
        self.output_window: ctk.CTkToplevel | None = None
        self.output: ctk.CTkTextbox | None = None
        self.latest_monitor_output = ""
        self.tray_icon = None
        self.tray_thread: threading.Thread | None = None

        self.status_filter_var    = tk.StringVar(value="All")
        self.severity_filter_var  = tk.StringVar(value="All")
        self.table_count_var      = tk.StringVar(value="")

        self.shift_hour_vars: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}
        self.shift_reminder_vars: dict[str, tk.StringVar] = {}
        self.shift_preview_var         = tk.StringVar(value="Reminder preview: —")
        self.auto_refresh_var          = tk.BooleanVar(value=True)
        self.refresh_interval_var      = tk.StringVar(value="30")
        self.refresh_interval_unit_var = tk.StringVar(value="Seconds")
        self.refresh_profile_var       = tk.StringVar(value="Balanced")
        self.cfg_mesdp_url_var         = tk.StringVar()
        self.cfg_mesdp_token_var       = tk.StringVar()
        self.cfg_smtp_server_var       = tk.StringVar()
        self.cfg_smtp_port_var         = tk.StringVar()
        self.cfg_smtp_tls_var          = tk.BooleanVar(value=True)
        self.cfg_email_sender_var      = tk.StringVar()
        self.cfg_email_password_var    = tk.StringVar()
        self.cfg_email_receivers_var   = tk.StringVar()
        self.cfg_email_receiver_input_var = tk.StringVar()
        self.cfg_email_receivers_list: list[str] = []
        self.cfg_receiver_listbox: tk.Listbox | None = None
        self._has_saved_mesdp_token    = False
        self._has_saved_email_password = False
        self.setup_required            = not runtime_config_ready(CONFIG)

        if self.setup_required:
            # Hide main dashboard until mandatory first-run setup is completed.
            self.root.withdraw()

        self._load_gui_preferences()
        self._configure_styles()
        self._build_ui()
        self._set_window_icon()
        self.root.protocol("WM_DELETE_WINDOW", self._on_main_window_close)
        self.root.bind_all("<Control-comma>", lambda _e: self.open_shift_settings_window())
        self._start_process_watchdog()
        self._update_shift_badge()
        if not self.setup_required:
            # After mainloop starts: worker threads call root.after() when done.
            self.root.after(200, lambda: self.refresh_status(force_data=True))
        else:
            self._set_output("Please complete first-time setup to start using the app.")
            self._open_runtime_config_window(force=True)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_gui_preferences(self) -> None:
        if not os.path.exists(self.gui_settings_file):
            return
        try:
            with open(self.gui_settings_file, "r", encoding="utf-8") as f:
                p = json.load(f)
            auto_refresh     = bool(p.get("auto_refresh", True))
            interval_seconds = max(5, min(86400, int(p.get("refresh_interval_seconds", 30))))
            interval_value   = str(p.get("refresh_interval_value", ""))
            interval_unit    = str(p.get("refresh_interval_unit", ""))
            refresh_profile  = str(p.get("refresh_profile", "Balanced"))
            self.auto_refresh_var.set(auto_refresh)
            if interval_value and interval_unit in {"Seconds", "Minutes", "Hours"}:
                self.refresh_interval_var.set(interval_value)
                self.refresh_interval_unit_var.set(interval_unit)
            else:
                self._set_interval_fields_from_seconds(interval_seconds)
            self.auto_refresh_ms = interval_seconds * 1000
            if refresh_profile in self.REFRESH_PROFILES or refresh_profile == "Custom":
                self.refresh_profile_var.set(refresh_profile)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def _save_gui_preferences(self) -> None:
        payload = {
            "auto_refresh":             bool(self.auto_refresh_var.get()),
            "refresh_interval_seconds": int(self.auto_refresh_ms / 1000),
            "refresh_interval_value":   self.refresh_interval_var.get(),
            "refresh_interval_unit":    self.refresh_interval_unit_var.get(),
            "refresh_profile":          self.refresh_profile_var.get(),
        }
        try:
            os.makedirs(os.path.dirname(self.gui_settings_file), exist_ok=True)
            with open(self.gui_settings_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_project_root(self) -> str:
        candidates = [
            os.path.dirname(os.path.abspath(__file__)),
            os.path.dirname(os.path.abspath(sys.executable)),
            os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))),
            os.getcwd(),
        ]
        required = ("start_scheduler.ps1", "stop_scheduler.ps1", "monitor_scheduler.ps1")
        for base in candidates:
            if all(os.path.exists(os.path.join(base, n)) for n in required):
                return base
        return candidates[0]

    def _center_toplevel(self, win: ctk.CTkToplevel, width: int, height: int) -> None:
        """Center popup windows relative to the main window for consistent placement."""
        try:
            self.root.update_idletasks()
            win.update_idletasks()
            scr_w = self.root.winfo_screenwidth()
            scr_h = self.root.winfo_screenheight()

            root_w = self.root.winfo_width()
            root_h = self.root.winfo_height()
            root_x = self.root.winfo_rootx()
            root_y = self.root.winfo_rooty()

            if root_w <= 1 or root_h <= 1:
                x = max(0, int((scr_w - width) / 2))
                y = max(0, int((scr_h - height) / 2))
            else:
                x = max(0, root_x + int((root_w - width) / 2))
                y = max(0, root_y + int((root_h - height) / 2))

            # Keep the window fully inside the current monitor bounds.
            x = min(max(0, x), max(0, scr_w - width))
            y = min(max(0, y), max(0, scr_h - height))

            win.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            win.geometry(f"{width}x{height}")

    def _center_existing_toplevel(self, win: ctk.CTkToplevel) -> None:
        """Center an already rendered toplevel using its actual scaled size."""
        try:
            self.root.update_idletasks()
            win.update_idletasks()
            scr_w = self.root.winfo_screenwidth()
            scr_h = self.root.winfo_screenheight()

            w = max(200, win.winfo_width())
            h = max(120, win.winfo_height())

            # Fit window to screen if DPI scaling makes it larger than visible area.
            max_w = max(320, scr_w - 40)
            max_h = max(240, scr_h - 80)
            fit_w = min(w, max_w)
            fit_h = min(h, max_h)
            if fit_w != w or fit_h != h:
                win.geometry(f"{fit_w}x{fit_h}")
                win.update_idletasks()
                w = max(200, win.winfo_width())
                h = max(120, win.winfo_height())

            root_w = self.root.winfo_width()
            root_h = self.root.winfo_height()
            root_x = self.root.winfo_rootx()
            root_y = self.root.winfo_rooty()

            if root_w <= 1 or root_h <= 1:
                x = max(0, int((scr_w - w) / 2))
                y = max(0, int((scr_h - h) / 2))
            else:
                x = max(0, root_x + int((root_w - w) / 2))
                y = max(0, root_y + int((root_h - h) / 2))

            # Keep the window fully inside the current monitor bounds.
            x = min(max(0, x), max(0, scr_w - w))
            y = min(max(0, y), max(0, scr_h - h))

            win.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # ── Process watchdog ─────────────────────────────────────────────────────

    def _parse_hhmm_minutes(self, value: str) -> int | None:
        try:
            hh_str, mm_str = value.split(":")
            hh, mm = int(hh_str), int(mm_str)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return hh * 60 + mm
        except (ValueError, AttributeError):
            return None
        return None

    def _get_current_shift(self) -> str:
        # Default shift windows if the settings file is missing/invalid.
        shift_windows = {
            "morning": ("08:00", "20:00"),
            "night": ("20:00", "08:00"),
        }
        try:
            # Auto-migrate from old location if new location doesn't exist
            if not os.path.exists(self.shift_settings_file):
                old_path = os.path.join(self.project_root, "shift_settings.json")
                if os.path.exists(old_path):
                    os.makedirs(os.path.dirname(self.shift_settings_file), exist_ok=True)
                    import shutil
                    shutil.copy2(old_path, self.shift_settings_file)
            
            with open(self.shift_settings_file, encoding="utf-8") as f:
                data = json.load(f)
            hours = data.get("shift_hours", {}) if isinstance(data, dict) else {}
            # Old 3-shift files ("evening" present) no longer apply: keep defaults.
            if isinstance(hours, dict) and "evening" not in hours:
                for shift in SHIFT_NAMES:
                    raw = hours.get(shift, {})
                    if isinstance(raw, dict):
                        start_time = str(raw.get("start_time", "")).strip()
                        end_time = str(raw.get("end_time", "")).strip()
                        if start_time and end_time:
                            shift_windows[shift] = (start_time, end_time)
        except Exception:
            pass

        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        for shift in SHIFT_NAMES:
            start_raw, end_raw = shift_windows[shift]
            start_min = self._parse_hhmm_minutes(start_raw)
            end_min = self._parse_hhmm_minutes(end_raw)
            if start_min is None or end_min is None:
                continue
            if start_min > end_min:  # crosses midnight
                if now_min >= start_min or now_min < end_min:
                    return shift
            else:
                if start_min <= now_min < end_min:
                    return shift
        return "morning"

    def _update_shift_badge(self) -> None:
        shift = self._get_current_shift()
        s, e, _ = self._shift_definitions().get(shift, ("", "", []))
        self.shift_badge.configure(text=f"{shift.upper()} SHIFT  ·  {s}–{e}",
                                   fg_color=NAVY_HVR, text_color=ON_NAVY)
        self._refresh_schedule_panel()
        self.root.after(20_000, self._update_shift_badge)

    def _start_process_watchdog(self) -> None:
        """Background thread: polls process state every 5s, updates UI on change."""
        def _poll():
            while True:
                time.sleep(5)
                try:
                    _, out = self._quick_check_process()
                    out_up = out.upper()
                    new_state = "RUNNING" if "RUNNING" in out_up and "NOT RUNNING" not in out_up else "NOT RUNNING"
                    if new_state != self.current_scheduler_state:
                        self.root.after(0, lambda s=new_state: self._on_watchdog_state_change(s))
                except Exception:
                    pass
        threading.Thread(target=_poll, daemon=True).start()

    def _quick_check_process(self) -> tuple[int, str]:
        project_root_ps = self.project_root.replace("'", "''")
        parent_root_ps = os.path.dirname(self.project_root).replace("'", "''")
        cmd = (
            f"$projectRoot='{project_root_ps}';"
            f"$parentRoot='{parent_root_ps}';"
            "$r=Get-CimInstance Win32_Process|Where-Object{"
            "$_.ProcessId -ne $PID -and "  # this query's own command line matches the patterns
            "$_.CommandLine -and "
            "("
            "$_.CommandLine -like '*worklog_reminder.py*--schedule*' -or "
            "$_.CommandLine -like '*MESDP Scheduler Control.exe*--schedule*'"
            ") -and ("
            "$_.CommandLine -like \"*$projectRoot*\" -or "
            "$_.CommandLine -like \"*$parentRoot*\""
            ")};"
            "if($r){'RUNNING'}else{'NOT RUNNING'}"
        )
        kw: dict = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kw["startupinfo"] = si
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd], **kw)
        return p.returncode, (p.stdout or "").strip()

    def _on_watchdog_state_change(self, new_state: str) -> None:
        self.current_scheduler_state = new_state
        self._show_scheduler_state(new_state)
        self.scheduler_detail.set(
            "Scheduler is running and will send the reminders above."
            if new_state == "RUNNING" else "Scheduler is stopped. Press Start scheduler to send reminders.")
        self._set_start_stop_button_for_state(new_state)

    def _show_scheduler_state(self, state: str) -> None:
        text, fg = {
            "RUNNING":     ("●  SCHEDULER RUNNING", GOOD),
            "NOT RUNNING": ("●  SCHEDULER STOPPED", BAD),
            "ERROR":       ("●  STATUS ERROR",      BAD),
        }.get(state, ("●  CHECKING", NAVY_HVR))
        self.scheduler_badge.configure(text=text, fg_color=fg, text_color=ON_NAVY)
        self._refresh_schedule_panel()

    def _make_card(self, parent) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=10,
                            border_width=1, border_color=BORDER)

    def _make_badge(self, parent, text: str, fg: str, txt: str = TEXT) -> ctk.CTkLabel:
        return ctk.CTkLabel(parent, text=text, fg_color=fg, text_color=txt,
                            font=(FONT_UI, 11, "bold"), corner_radius=6,
                            padx=10, pady=3)

    def _section_title(self, parent, text: str, note_var: tk.StringVar | None = None) -> ctk.CTkFrame:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(row, text=text.upper(), text_color=NAVY,
                     font=(FONT_UI, 12, "bold")).pack(side=tk.LEFT)
        if note_var is not None:
            ctk.CTkLabel(row, textvariable=note_var, text_color=TEXT_MUTED,
                         font=(FONT_UI, 11)).pack(side=tk.RIGHT)
        return row

    # ── Styles ────────────────────────────────────────────────────────────────

    def _configure_styles(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.root.configure(fg_color=BG)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Tickets.Treeview",
            background=SURFACE, foreground=TEXT,
            fieldbackground=SURFACE, bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
            borderwidth=0, rowheight=34, font=(FONT_UI, 10))
        style.configure("Tickets.Treeview.Heading",
            background=HEAD_BG, foreground=TEXT_MUTED,
            bordercolor=BORDER, lightcolor=HEAD_BG, darkcolor=HEAD_BG,
            font=(FONT_UI, 9, "bold"), relief="flat", padding=(10, 8))
        style.layout("Tickets.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.map("Tickets.Treeview",
            background=[("selected", NAVY_SOFT)],
            foreground=[("selected", TEXT)])
        style.map("Tickets.Treeview.Heading",
            background=[("active", BORDER_LT)])
        style.configure("Tickets.Vertical.TScrollbar",
            background=BORDER_LT, troughcolor=SURFACE, bordercolor=SURFACE,
            lightcolor=BORDER_LT, darkcolor=BORDER_LT, arrowcolor=TEXT_MUTED,
            gripcount=0, arrowsize=12)

    # ── UI Build ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header band
        header = ctk.CTkFrame(self.root, fg_color=NAVY, corner_radius=0)
        header.pack(fill=tk.X)
        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side=tk.LEFT, padx=28, pady=(10, 12))
        ctk.CTkLabel(brand, text="CLL SYSTEMS  ·  MESDP", text_color=ON_NAVY_MUTED,
                     font=(FONT_UI, 11, "bold")).pack(anchor="w")
        ctk.CTkLabel(brand, text="Worklog Reminder Control", text_color=ON_NAVY,
                     font=(FONT_UI, 22, "bold")).pack(anchor="w")

        hright = ctk.CTkFrame(header, fg_color="transparent")
        hright.pack(side=tk.RIGHT, padx=28)
        self.header_clock_var = tk.StringVar(value="")
        ctk.CTkLabel(hright, textvariable=self.header_clock_var, text_color=ON_NAVY_MUTED,
                     font=(FONT_UI, 12)).pack(anchor="e", pady=(0, 6))
        pills = ctk.CTkFrame(hright, fg_color="transparent")
        pills.pack(anchor="e")
        self.shift_badge = self._make_badge(pills, "DETECTING SHIFT", NAVY_HVR, ON_NAVY)
        self.shift_badge.pack(side=tk.LEFT, padx=(0, 8))
        self.scheduler_badge = self._make_badge(pills, "CHECKING", NAVY_HVR, ON_NAVY)
        self.scheduler_badge.pack(side=tk.LEFT)

        outer = ctk.CTkFrame(self.root, fg_color=BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=28, pady=(16, 20))

        # Toolbar
        toolbar = ctk.CTkFrame(outer, fg_color="transparent")
        toolbar.pack(fill=tk.X, pady=(0, 14))

        btn = dict(height=36, corner_radius=8, font=(FONT_UI, 12, "bold"))
        outline = dict(fg_color=SURFACE, hover_color=BORDER_LT, text_color=TEXT,
                       border_width=1, border_color=BORDER)

        self.start_stop_btn = ctk.CTkButton(
            toolbar, text="▶  Start scheduler", width=170, fg_color=GOOD, hover_color=GOOD_HVR,
            text_color=ON_NAVY, command=self.toggle_scheduler, **btn)
        self.run_now_btn = ctk.CTkButton(
            toolbar, text="Send reminder now", width=160, command=self.run_one_time_report,
            **btn, **outline)
        self.refresh_btn = ctk.CTkButton(
            toolbar, text="↻  Refresh", width=110,
            command=lambda: self.refresh_status(force_data=True), **btn, **outline)
        self.settings_btn = ctk.CTkButton(
            toolbar, text="Shift settings", width=128, command=self.open_shift_settings_window,
            **btn, **outline)
        self.app_config_btn = ctk.CTkButton(
            toolbar, text="Connection", width=118,
            command=lambda: self._open_runtime_config_window(force=False), **btn, **outline)
        self.terminal_btn = ctk.CTkButton(
            toolbar, text="Log", width=70, command=self.open_output_window, **btn, **outline)

        self.start_stop_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.run_now_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.refresh_btn.pack(side=tk.LEFT, padx=(0, 18))
        ctk.CTkFrame(toolbar, fg_color=BORDER, width=1, height=28).pack(side=tk.LEFT, padx=(0, 18))
        for b in (self.settings_btn, self.app_config_btn, self.terminal_btn):
            b.pack(side=tk.LEFT, padx=(0, 8))

        meta = ctk.CTkFrame(toolbar, fg_color="transparent")
        meta.pack(side=tk.RIGHT)
        self.auto_refresh_info_var = tk.StringVar(value="Auto refresh: 30 secs")
        ctk.CTkLabel(meta, textvariable=self.auto_refresh_info_var, text_color=TEXT_MUTED,
                     font=(FONT_UI, 11)).pack(anchor="e")
        self.auto_refresh_count_var = tk.StringVar(value="")
        ctk.CTkLabel(meta, textvariable=self.auto_refresh_count_var, text_color=TEXT_DIM,
                     font=(FONT_UI, 11)).pack(anchor="e")

        # KPI row (same figures as the reminder email, PM tickets excluded)
        kpis = ctk.CTkFrame(outer, fg_color="transparent")
        kpis.pack(fill=tk.X, pady=(0, 14))
        self.kpi: dict[str, tuple[ctk.CTkLabel, tk.StringVar, str]] = {}
        specs = [
            ("in_progress", "In Progress",     NAVY,  "Open queue"),
            ("on_hold",     "On Hold",         WARN,  "Open queue"),
            ("pending",     "Pending worklog", NAVY,  "No update this shift"),
            ("no_record",   "No worklog",      BAD,   "Never updated"),
            ("stale",       "3+ days old",     WARN,  "Last worklog"),
            ("opened",      "Opened",          NAVY,  "This shift"),
            ("closed",      "Closed",          GOOD,  "This shift"),
        ]
        for i, (key, label, colour, caption) in enumerate(specs):
            kpis.grid_columnconfigure(i, weight=1, uniform="kpi")
            card = self._make_card(kpis)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            ctk.CTkFrame(card, fg_color=colour, height=4, corner_radius=0).pack(
                fill=tk.X, padx=1, pady=(1, 0))
            ctk.CTkLabel(card, text=label.upper(), text_color=TEXT_MUTED,
                         font=(FONT_UI, 10, "bold")).pack(anchor="w", padx=14, pady=(8, 0))
            value = ctk.CTkLabel(card, text="–", text_color=TEXT_DIM, font=(FONT_UI, 26, "bold"), height=34)
            value.pack(anchor="w", padx=14)
            cap = tk.StringVar(value=caption)
            ctk.CTkLabel(card, textvariable=cap, text_color=TEXT_DIM,
                         font=(FONT_UI, 10)).pack(anchor="w", padx=14, pady=(0, 8))
            self.kpi[key] = (value, cap, colour)

        # Body: schedule + roster in a sidebar, ticket table takes the rest
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill=tk.BOTH, expand=True)
        body.grid_columnconfigure(0, weight=0, minsize=430)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        side = ctk.CTkFrame(body, fg_color="transparent", width=430)
        side.grid(row=0, column=0, sticky="nsew", padx=(0, 16))

        sc = self._make_card(side)
        sc.pack(fill=tk.X, pady=(0, 14))
        self._section_title(sc, "Reminder schedule").pack(fill=tk.X, padx=18, pady=(14, 8))
        nxt = ctk.CTkFrame(sc, fg_color=NAVY_SOFT, corner_radius=8)
        nxt.pack(fill=tk.X, padx=18, pady=(0, 10))
        self.next_reminder_var = tk.StringVar(value="–")
        self.next_reminder_eta_var = tk.StringVar(value="")
        ctk.CTkLabel(nxt, text="NEXT REMINDER", text_color=TEXT_MUTED,
                     font=(FONT_UI, 10, "bold")).pack(anchor="w", padx=14, pady=(8, 0))
        ctk.CTkLabel(nxt, textvariable=self.next_reminder_var, text_color=NAVY,
                     font=(FONT_UI, 18, "bold"), height=28).pack(anchor="w", padx=14)
        ctk.CTkLabel(nxt, textvariable=self.next_reminder_eta_var, text_color=TEXT_MUTED,
                     font=(FONT_UI, 11)).pack(anchor="w", padx=14, pady=(0, 8))
        self.schedule_rows = ctk.CTkFrame(sc, fg_color="transparent")
        self.schedule_rows.pack(fill=tk.X, padx=18, pady=(0, 14))
        self._schedule_rows_key = None
        self.scheduler_detail = tk.StringVar(value="Checking scheduler…")  # shown in the log only

        rc = self._make_card(side)
        rc.pack(fill=tk.BOTH, expand=True)
        self.roster_note_var = tk.StringVar(value="")  # set once data arrives
        self._section_title(rc, "Duty roster", self.roster_note_var).pack(fill=tk.X, padx=18, pady=(14, 4))
        self.roster_body = ctk.CTkScrollableFrame(rc, fg_color="transparent", height=120,
                                                  scrollbar_button_color=BORDER_LT,
                                                  scrollbar_button_hover_color=BORDER)
        self.roster_body.pack(fill=tk.BOTH, expand=True, padx=(12, 6), pady=(0, 10))
        self._render_roster(None, loading=True)

        # Ticket table
        tcard = self._make_card(body)
        tcard.grid(row=0, column=1, sticky="nsew")
        trow = self._section_title(tcard, "Tickets pending a worklog", self.table_count_var)
        trow.pack(fill=tk.X, padx=18, pady=(14, 10))
        self.data_updated_var = tk.StringVar(value="")
        ctk.CTkLabel(trow, textvariable=self.data_updated_var, text_color=TEXT_DIM,
                     font=(FONT_UI, 11)).pack(side=tk.RIGHT, padx=(0, 14))

        fbar = ctk.CTkFrame(tcard, fg_color="transparent")
        fbar.pack(fill=tk.X, padx=18, pady=(0, 10))
        combo_cfg = dict(width=170, height=32, font=(FONT_UI, 11), dropdown_font=(FONT_UI, 11),
                         fg_color=SURFACE, border_color=BORDER, border_width=1,
                         button_color=BORDER_LT, button_hover_color=BORDER, text_color=TEXT,
                         dropdown_fg_color=SURFACE, dropdown_text_color=TEXT,
                         dropdown_hover_color=NAVY_SOFT, corner_radius=6)

        ctk.CTkLabel(fbar, text="Status", text_color=TEXT_MUTED,
                     font=(FONT_UI, 11)).pack(side=tk.LEFT)
        self.status_filter_combo = ctk.CTkComboBox(
            fbar, values=["All"], variable=self.status_filter_var,
            state="readonly", command=lambda _v: self._on_filter_changed(), **combo_cfg)
        self.status_filter_combo.pack(side=tk.LEFT, padx=(8, 18))
        self.status_filter_combo.set("All")

        ctk.CTkLabel(fbar, text="Severity", text_color=TEXT_MUTED,
                     font=(FONT_UI, 11)).pack(side=tk.LEFT)
        self.severity_filter_combo = ctk.CTkComboBox(
            fbar, values=["All"], variable=self.severity_filter_var,
            state="readonly", command=lambda _v: self._on_filter_changed(), **combo_cfg)
        self.severity_filter_combo.pack(side=tk.LEFT, padx=(8, 18))
        self.severity_filter_combo.set("All")

        ctk.CTkButton(fbar, text="Clear filters", width=100, height=32, corner_radius=6,
                      fg_color=SURFACE, hover_color=BORDER_LT, text_color=TEXT_MUTED,
                      border_width=1, border_color=BORDER, font=(FONT_UI, 11),
                      command=self._clear_filters).pack(side=tk.LEFT)

        border = ctk.CTkFrame(tcard, fg_color=BORDER, corner_radius=0)
        border.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 16))
        self.table_wrap = ctk.CTkFrame(border, fg_color=SURFACE, corner_radius=0)
        self.table_wrap.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        cols    = ("ticket_id", "title", "assigned_l1", "severity", "status", "last_worklog")
        headers = {"ticket_id": "TICKET", "title": "TITLE", "assigned_l1": "ASSIGNED TO",
                   "severity": "SEVERITY", "status": "STATUS", "last_worklog": "LAST WORKLOG"}
        widths  = {"ticket_id": 90, "title": 360, "assigned_l1": 150,
                   "severity": 100, "status": 120, "last_worklog": 150}
        # Relative sizing to keep the table readable without requiring horizontal drag.
        self.ticket_column_weights = {
            "ticket_id": 9,
            "title": 39,
            "assigned_l1": 16,
            "severity": 10,
            "status": 11,
            "last_worklog": 15,
        }
        self.ticket_column_widths = widths

        self.ticket_table = ttk.Treeview(
            self.table_wrap, columns=cols, show="headings", style="Tickets.Treeview")
        self.ticket_table.tag_configure("row_even", background=SURFACE)
        self.ticket_table.tag_configure("row_odd", background=ZEBRA)
        self.ticket_table.tag_configure("no_worklog", foreground=BAD)
        self.ticket_table.tag_configure("stale", foreground=WARN)
        left_cols = {"title", "assigned_l1"}
        for col in cols:
            anchor = tk.W if col in left_cols else tk.CENTER
            self.ticket_table.heading(col, text=headers[col], anchor=anchor)
            min_w = self.TITLE_MIN_WIDTH if col == "title" else max(80, int(widths[col] * 0.7))
            self.ticket_table.column(col, width=widths[col], minwidth=min_w,
                                     stretch=(col == "title"), anchor=anchor)

        sy = ttk.Scrollbar(self.table_wrap, orient=tk.VERTICAL, command=self.ticket_table.yview,
                           style="Tickets.Vertical.TScrollbar")
        self.ticket_table.configure(yscrollcommand=sy.set)
        self.ticket_table.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        self.table_wrap.grid_rowconfigure(0, weight=1)
        self.table_wrap.grid_columnconfigure(0, weight=1)
        self.table_empty_var = tk.StringVar(value="Loading ticket data from MESDP…")
        self.table_empty_label = ctk.CTkLabel(self.table_wrap, textvariable=self.table_empty_var,
                                              text_color=TEXT_MUTED, fg_color=SURFACE,
                                              font=(FONT_UI, 13))
        self.table_empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.ticket_table.bind("<Button-1>", self._block_table_separator_resize, add="+")
        self.ticket_table.bind("<Configure>", self._on_ticket_table_configure)
        self.ticket_table.bind("<ButtonRelease-1>", self._on_ticket_table_mouse_release)
        self.ticket_table.bind("<Motion>",    self._on_ticket_table_hover)
        self.ticket_table.bind("<Leave>",     self._hide_title_tooltip)
        self._refresh_schedule_panel()

    # ── Dashboard panels ──────────────────────────────────────────────────────

    def _shift_definitions(self) -> dict[str, tuple[str, str, list[int]]]:
        """{shift: (start, end, reminder offsets)} from the shift settings file."""
        defs = {"morning": ("08:00", "20:00", [60, 30]), "night": ("20:00", "08:00", [60, 30])}
        try:
            with open(self.shift_settings_file, encoding="utf-8") as f:
                hours = (json.load(f) or {}).get("shift_hours", {})
            if isinstance(hours, dict) and "evening" not in hours:
                for sn in SHIFT_NAMES:
                    raw = hours.get(sn)
                    if not isinstance(raw, dict):
                        continue
                    s, e = str(raw.get("start_time", "")).strip(), str(raw.get("end_time", "")).strip()
                    offs = raw.get("reminder_minutes", defs[sn][2])
                    if isinstance(offs, (int, float)):
                        offs = [offs]
                    offs = [int(o) for o in offs if isinstance(o, (int, float))] or defs[sn][2]
                    if self._parse_hhmm_minutes(s) is not None and self._parse_hhmm_minutes(e) is not None:
                        defs[sn] = (s, e, sorted(set(offs), reverse=True))
        except Exception:
            pass
        return defs

    def _reminder_slots(self) -> list[tuple[int, str, bool]]:
        """[(minute of day, shift, is_final)] for every scheduled reminder."""
        slots = []
        for sn, (_s, e, offs) in self._shift_definitions().items():
            end = self._parse_hhmm_minutes(e) or 0
            for i, off in enumerate(offs):
                slots.append(((end - off) % 1440, sn, len(offs) > 1 and i == len(offs) - 1))
        return sorted(slots)

    def _refresh_schedule_panel(self) -> None:
        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        slots = self._reminder_slots()
        if slots:
            upcoming = [s for s in slots if s[0] > now_min] or [slots[0]]
            at, sn, final = upcoming[0]
            delta = (at - now_min) % 1440 or 1440
            hh, mm = divmod(at, 60)
            self.next_reminder_var.set(f"{hh:02d}:{mm:02d}  ·  {sn.capitalize()}{'  (final)' if final else ''}")
            eta = f"{delta // 60}h {delta % 60:02d}m" if delta >= 60 else f"{delta} min"
            running = self.current_scheduler_state == "RUNNING"
            self.next_reminder_eta_var.set(
                f"in {eta}" if running else f"in {eta}  ·  scheduler is stopped, nothing will be sent")
        rows = []
        for sn, (s, e, _offs) in self._shift_definitions().items():
            times = [f"{(m // 60):02d}:{(m % 60):02d}" for m, n, _f in slots if n == sn]
            rows.append((sn.capitalize(), f"{s} – {e}", "  ·  ".join(times)))
        if rows != self._schedule_rows_key:
            self._schedule_rows_key = rows
            for w in self.schedule_rows.winfo_children():
                w.destroy()
            for c, head in enumerate(("SHIFT", "HOURS", "REMINDERS")):
                ctk.CTkLabel(self.schedule_rows, text=head, text_color=TEXT_MUTED,
                             font=(FONT_UI, 10, "bold")).grid(row=0, column=c, sticky="w", padx=(0, 28))
            for r, row in enumerate(rows, start=1):
                for c, val in enumerate(row):
                    ctk.CTkLabel(self.schedule_rows, text=val, text_color=TEXT,
                                 font=(FONT_UI, 12, "bold" if c == 0 else "normal")).grid(
                        row=r, column=c, sticky="w", padx=(0, 28))
        self.header_clock_var.set(now.strftime("%a %d %b %Y  ·  %H:%M"))

    def _render_roster(self, roster, loading: bool = False) -> None:
        for w in self.roster_body.winfo_children():
            w.destroy()

        def note(text: str) -> None:
            ctk.CTkLabel(self.roster_body, text=text, text_color=TEXT_MUTED,
                         font=(FONT_UI, 12)).pack(anchor="w", pady=(4, 0))

        if loading:
            note("Loading roster…"); return
        if roster == "error":
            note("Roster not loaded (data pull failed)."); return
        if roster is None:
            note("No roster file configured."); return
        if not isinstance(roster, dict) or roster.get("unavailable"):
            note("Roster file could not be read."); return

        for idx, (kind, res) in enumerate((("Current shift", roster.get("current")),
                                           ("Next shift", roster.get("next")))):
            block = ctk.CTkFrame(self.roster_body, fg_color="transparent")
            block.pack(fill=tk.X, pady=(0 if idx == 0 else 10, 0))
            head = kind.upper()
            if res:
                head += f"   {str(res.get('shift') or '').capitalize()}"
                if res.get("date"):
                    try:
                        head += f"  ·  {datetime.strptime(res['date'][:10], '%Y-%m-%d'):%a %d %b}"
                    except ValueError:
                        pass
            ctk.CTkLabel(block, text=head, text_color=NAVY if idx == 0 else TEXT_MUTED,
                         font=(FONT_UI, 10, "bold")).pack(anchor="w")
            if not res:
                ctk.CTkLabel(block, text="Not found in the roster for this date.",
                             text_color=TEXT_MUTED, font=(FONT_UI, 12)).pack(anchor="w")
                continue
            people = res.get("people") or []
            if not people:
                ctk.CTkLabel(block, text="Nobody rostered.", text_color=TEXT_MUTED,
                             font=(FONT_UI, 12)).pack(anchor="w")
                continue
            tiers: dict[str, list[dict]] = {}
            for p in people:
                tiers.setdefault(p.get("tier") or "Team", []).append(p)
            for tier, members in tiers.items():
                row = ctk.CTkFrame(block, fg_color="transparent")
                row.pack(anchor="w", fill=tk.X, pady=(3, 0))
                ctk.CTkLabel(row, text=tier, width=52, anchor="nw", text_color=TEXT_MUTED,
                             font=(FONT_UI, 11, "bold")).pack(side=tk.LEFT, anchor="n", pady=(3, 0))
                chips = ctk.CTkFrame(row, fg_color="transparent")
                chips.pack(side=tk.LEFT, fill=tk.X, expand=True)
                line = None
                for n, p in enumerate(members):
                    if n % 3 == 0:
                        line = ctk.CTkFrame(chips, fg_color="transparent")
                        line.pack(anchor="w", pady=(0, 4))
                    first = bool(p.get("first_contact"))
                    if first and idx == 0:
                        chip = self._make_badge(line, f"{p.get('name')}  ·  first contact", WARN_DIM, WARN)
                    else:
                        chip = ctk.CTkLabel(line, text=str(p.get("name")), corner_radius=6, padx=10, pady=3,
                                            fg_color=NAVY_SOFT if idx == 0 else SURFACE_ALT,
                                            text_color=TEXT if idx == 0 else TEXT_MUTED,
                                            font=(FONT_UI, 11, "bold" if first else "normal"))
                    chip.pack(side=tk.LEFT, padx=(0, 6))

    def _set_kpis(self, values: dict, captions: dict | None = None) -> None:
        for key, (label, cap, colour) in self.kpi.items():
            v = values.get(key)
            if v is None:
                label.configure(text="–", text_color=TEXT_DIM)
            else:
                quiet = v == 0 and key in {"no_record", "stale", "pending", "on_hold"}
                label.configure(text=str(v), text_color=TEXT_DIM if quiet else colour)
            if captions and key in captions:
                cap.set(captions[key])

    def open_output_window(self) -> None:
        if self.output_window and self.output_window.winfo_exists():
            self.output_window.lift()
            self.output_window.focus_force()
            return

        win = ctk.CTkToplevel(self.root)
        win.title("Scheduler Log")
        win.geometry("920x360")
        win.minsize(760, 260)
        win.configure(fg_color=SURFACE)
        win.transient(self.root)
        win.lift()
        win.focus_force()
        self.output_window = win

        c = ctk.CTkFrame(win, fg_color=SURFACE)
        c.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        ctk.CTkLabel(c, text="SCHEDULER LOG", text_color=NAVY,
                     font=(FONT_MONO, 10, "bold")).pack(anchor="w", pady=(0, 8))
        self.output = ctk.CTkTextbox(c, font=(FONT_MONO, 11),
                                     fg_color="#101828", text_color="#d0d5dd",
                                     border_width=1, border_color=BORDER, corner_radius=8)
        self.output.pack(fill=tk.BOTH, expand=True)
        self.output.configure(state=tk.NORMAL)
        self.output.delete("1.0", tk.END)
        self.output.insert("1.0", self.latest_monitor_output or "No monitor output yet.")
        self.output.configure(state=tk.DISABLED)

        win.protocol("WM_DELETE_WINDOW", self._close_output_window)

    def _close_output_window(self) -> None:
        if self.output_window and self.output_window.winfo_exists():
            self.output_window.destroy()
        self.output_window = None
        self.output = None

    # ── Table ─────────────────────────────────────────────────────────────────

    def _on_ticket_table_configure(self, _event=None) -> None:
        if self._table_resize_job:
            self.root.after_cancel(self._table_resize_job)
        self._table_resize_job = self.root.after(120, self._apply_ticket_table_layout)

    def _apply_ticket_table_layout(self) -> None:
        self._table_resize_job = None
        self._resize_ticket_columns()
        if self.latest_tickets:
            self._render_filtered_ticket_rows()

    def _on_ticket_table_mouse_release(self, event) -> None:
        try:
            if self.ticket_table.identify_region(event.x, event.y) == "separator":
                self._user_resized_columns = True
        except Exception:
            pass

    def _block_table_separator_resize(self, event):
        """Prevent manual column-separator dragging that causes unstable expansion."""
        try:
            if self.ticket_table.identify_region(event.x, event.y) == "separator":
                return "break"
        except Exception:
            return None
        return None

    def _resize_ticket_columns(self) -> None:
        if self._user_resized_columns:
            return
        avail = self.table_wrap.winfo_width()
        if avail <= 1:
            return
        usable = max(640, avail - 12)
        total_weight = sum(self.ticket_column_weights.values())
        for col, w in self.ticket_column_weights.items():
            width = int((usable * w) / total_weight)
            if col == "title":
                width = max(self.TITLE_MIN_WIDTH, min(self.TITLE_MAX_WIDTH, width))
            else:
                min_w = max(80, int(self.ticket_column_widths[col] * 0.7))
                width = max(min_w, width)
            self.ticket_table.column(col, width=width, stretch=(col == "title"))

    def _resize_title_column(self) -> None:
        """Backward-compatible alias for older call sites."""
        self._resize_ticket_columns()

    def _truncate_title_for_current_width(self, text: str) -> str:
        px     = int(self.ticket_table.column("title", "width"))
        max_ch = min(self.TITLE_MAX_CHARS, max(20, int((px - 20) / 7)))
        return text if len(text) <= max_ch else text[:max_ch - 3].rstrip() + "…"

    def _show_title_tooltip(self, full_title: str, x: int, y: int) -> None:
        if self._title_tooltip is None or not self._title_tooltip.winfo_exists():
            tip = ctk.CTkToplevel(self.root)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.configure(fg_color=SURFACE_ALT)
            lbl = ctk.CTkLabel(tip, text=full_title, text_color=TEXT,
                                fg_color=SURFACE_ALT, justify=tk.LEFT,
                                wraplength=520, padx=12, pady=8, font=(FONT_UI, 10))
            lbl.pack()
            self._title_tooltip       = tip
            self._title_tooltip_label = lbl
        if self._title_tooltip_label:
            self._title_tooltip_label.configure(text=full_title)
        self._title_tooltip.geometry(f"+{x + 14}+{y + 18}")
        self._title_tooltip.deiconify()

    def _hide_title_tooltip(self, _event=None) -> None:
        self._title_hover_item = None
        if self._title_tooltip and self._title_tooltip.winfo_exists():
            self._title_tooltip.withdraw()

    def _on_ticket_table_hover(self, event) -> None:
        row_id = self.ticket_table.identify_row(event.y)
        col_id = self.ticket_table.identify_column(event.x)
        if not row_id or col_id != "#2":
            self._hide_title_tooltip(); return
        full = self._full_title_by_item.get(row_id, "")
        if not full:
            self._hide_title_tooltip(); return
        vals = self.ticket_table.item(row_id, "values")
        if len(vals) > 1 and vals[1] == full:
            self._hide_title_tooltip(); return
        if self._title_hover_item != row_id:
            self._title_hover_item = row_id
            self._show_title_tooltip(full, event.x_root, event.y_root)
        elif self._title_tooltip and self._title_tooltip.winfo_exists():
            self._title_tooltip.geometry(f"+{event.x_root + 14}+{event.y_root + 18}")

    def _refresh_filter_options(self, tickets: list[dict]) -> None:
        statuses   = sorted({str(t.get("status",   "")).strip() for t in tickets if t.get("status")})
        severities = sorted({str(t.get("severity", "")).strip() for t in tickets if t.get("severity")})
        self.status_filter_combo.configure(values=["All", *statuses])
        self.severity_filter_combo.configure(values=["All", *severities])
        if self.status_filter_var.get() not in ["All", *statuses]:
            self.status_filter_var.set("All"); self.status_filter_combo.set("All")
        if self.severity_filter_var.get() not in ["All", *severities]:
            self.severity_filter_var.set("All"); self.severity_filter_combo.set("All")

    def _get_filtered_tickets(self) -> list[dict]:
        sel_s, sel_v = self.status_filter_var.get().strip(), self.severity_filter_var.get().strip()
        out = self.latest_tickets
        if sel_s and sel_s != "All":
            out = [t for t in out if str(t.get("status",   "")).strip() == sel_s]
        if sel_v and sel_v != "All":
            out = [t for t in out if str(t.get("severity", "")).strip() == sel_v]
        return out

    def _on_filter_changed(self) -> None:
        self._render_filtered_ticket_rows()

    def _clear_filters(self) -> None:
        self.status_filter_var.set("All");   self.status_filter_combo.set("All")
        self.severity_filter_var.set("All"); self.severity_filter_combo.set("All")
        self._render_filtered_ticket_rows()

    def _render_filtered_ticket_rows(self) -> None:
        filtered = self._get_filtered_tickets()
        self._render_ticket_rows(filtered)
        n, total = len(filtered), len(self.latest_tickets)
        self.table_count_var.set(f"{total} ticket{'' if total == 1 else 's'}" if n == total else f"Showing {n} of {total}")

    def _render_ticket_rows(self, tickets: list[dict]) -> None:
        self._hide_title_tooltip()
        self._full_title_by_item.clear()
        for row in self.ticket_table.get_children():
            self.ticket_table.delete(row)
        if tickets:
            self.table_empty_label.place_forget()
        else:
            if self.latest_tickets:
                self.table_empty_var.set("No tickets match the selected filters.")
            self.table_empty_label.place(relx=0.5, rely=0.5, anchor="center")
        today = datetime.now().date()
        for idx, item in enumerate(tickets):
            raw   = str(item.get("title", "N/A"))
            short = self._truncate_title_for_current_width(raw)
            last_worklog = str(item.get("last_worklog", "N/A"))
            row_tag = "row_even" if idx % 2 == 0 else "row_odd"
            tags = [row_tag]
            try:
                lw_date = datetime.strptime(last_worklog, "%Y-%m-%d").date()
                days = (today - lw_date).days
                last_worklog = (f"{lw_date:%d %b}  ·  {days}d ago" if days
                                else f"Today {item.get('last_worklog_time', '')}".strip())
                if days >= 3:
                    tags.append("stale")
            except ValueError:
                if last_worklog.strip().lower() in {"no record", "n/a", "na"}:
                    last_worklog = "No worklog"
                    tags.append("no_worklog")
            rid   = self.ticket_table.insert("", tk.END, values=(
                item.get("ticket_id",    "N/A"),
                short,
                item.get("assigned_l1",  "N/A"),
                item.get("severity",     "N/A"),
                item.get("status",       "N/A"),
                last_worklog,
            ), tags=tuple(tags))
            self._full_title_by_item[rid] = raw

    # ── PowerShell / Python runners ───────────────────────────────────────────

    def _run_ps(self, script_path: str) -> tuple[int, str]:
        if not os.path.exists(script_path):
            return 1, f"Script not found: {script_path}"
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path]
        kw: dict = {"cwd": self.project_root, "capture_output": True,
                    "text": True, "encoding": "utf-8", "errors": "replace"}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kw["startupinfo"] = si
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            p = subprocess.run(cmd, timeout=30, **kw)
            return p.returncode, ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()
        except subprocess.TimeoutExpired:
            return 1, f"PowerShell timeout while running: {os.path.basename(script_path)}"
        except Exception as exc:
            return 1, f"PowerShell execution failed: {exc}"

    def _run_python_inline(self, code_text: str, timeout_sec: int = 30) -> tuple[int, str]:
        if not os.path.exists(self.python_exe):
            # Frozen/installed build may not ship an external python.exe.
            # Execute trusted internal snippets directly in-process as fallback,
            # but bound the wait so refresh/action UI cannot freeze indefinitely.
            result: dict[str, object] = {"rc": 1, "out": "Inline execution did not run."}

            def _runner() -> None:
                buf = io.StringIO()
                try:
                    ns: dict = {}
                    with redirect_stdout(buf):
                        exec(code_text, ns, ns)
                    result["rc"] = 0
                    result["out"] = (buf.getvalue() or "OK").strip()
                except Exception:
                    result["rc"] = 1
                    result["out"] = traceback.format_exc()

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join(max(1, timeout_sec))
            if t.is_alive():
                return 1, f"Inline Python timeout after {timeout_sec}s"
            return int(result.get("rc", 1)), str(result.get("out", ""))
        # Code goes in on stdin, not with -c: inline snippets can contain the
        # MESDP token / SMTP password, and command lines are readable by other
        # processes on the machine.
        cmd = [self.python_exe, "-X", "utf8", "-"]
        kw: dict = {"cwd": self.project_root, "capture_output": True,
                    "text": True, "encoding": "utf-8", "errors": "replace"}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kw["startupinfo"] = si
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            p = subprocess.run(cmd, input=code_text, timeout=max(1, timeout_sec), **kw)
            return p.returncode, ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()
        except subprocess.TimeoutExpired:
            return 1, f"Python inline timeout after {timeout_sec}s"
        except Exception as exc:
            return 1, f"Python inline execution failed: {exc}"

    # ── Interval helpers ──────────────────────────────────────────────────────

    def _set_interval_fields_from_seconds(self, seconds: int) -> None:
        if seconds % 3600 == 0:
            self.refresh_interval_var.set(str(seconds // 3600))
            self.refresh_interval_unit_var.set("Hours")
        elif seconds % 60 == 0:
            self.refresh_interval_var.set(str(seconds // 60))
            self.refresh_interval_unit_var.set("Minutes")
        else:
            self.refresh_interval_var.set(str(seconds))
            self.refresh_interval_unit_var.set("Seconds")

    def _sync_refresh_combo_to_vars(self) -> None:
        if hasattr(self, "refresh_interval_combo"):
            self.refresh_interval_var.set(self.refresh_interval_combo.get().strip())
        if hasattr(self, "refresh_unit_combo"):
            self.refresh_interval_unit_var.set(self.refresh_unit_combo.get().strip())
        if hasattr(self, "refresh_profile_combo"):
            self.refresh_profile_var.set(self.refresh_profile_combo.get().strip())

    def _interval_seconds_from_fields(self) -> int:
        self._sync_refresh_combo_to_vars()
        unit = self.refresh_interval_unit_var.get().strip()
        val  = int(self.refresh_interval_var.get().strip())
        return val * (3600 if unit == "Hours" else 60 if unit == "Minutes" else 1)

    # ── Settings window ───────────────────────────────────────────────────────

    def _load_shift_hours_into_form(self) -> None:
        if not self.shift_hour_vars:
            return
        code = (
            "import json, sys\n"
            f"sys.path.insert(0, {self.project_root!r})\n"
            "import worklog_reminder as wr\n"
            "out={'times':wr.SHIFT_TIMES,'reminders':wr.SHIFT_REMINDER_TIMES,'offsets':wr.SHIFT_REMINDER_OFFSETS}\n"
            "print(json.dumps(out))\n"
        )
        rc, out = self._run_python_inline(code)
        if rc != 0:
            return
        parsed = None
        for line in reversed(out.splitlines()):
            try:
                parsed = json.loads(line); break
            except json.JSONDecodeError:
                continue
        if not isinstance(parsed, dict):
            return
        times   = parsed.get("times", {})
        offsets = parsed.get("offsets", {})
        for sn, (sv, ev) in self.shift_hour_vars.items():
            val = times.get(sn, ["00:00", "00:00"])
            if isinstance(val, (list, tuple)) and len(val) == 2:
                sv.set(str(val[0])); ev.set(str(val[1]))
            if sn in self.shift_reminder_vars:
                val = offsets.get(sn, [60, 30])
                self.shift_reminder_vars[sn].set(", ".join(map(str, val)) if isinstance(val, (list, tuple)) else str(val))
        self._refresh_shift_preview(parsed.get("reminders", {}))

    def _open_runtime_config_window(self, force: bool = False) -> None:
        if self.config_window and self.config_window.winfo_exists():
            self.config_window.lift(); self.config_window.focus_force(); return

        email_cfg = CONFIG.get("email", {}) if isinstance(CONFIG, dict) else {}
        mesdp_cfg = CONFIG.get("mesdp", {}) if isinstance(CONFIG, dict) else {}

        self.cfg_mesdp_url_var.set(str(mesdp_cfg.get("base_url", "")))
        self._has_saved_mesdp_token = bool(str(mesdp_cfg.get("auth_token", "")).strip())
        self.cfg_mesdp_token_var.set("")
        self.cfg_smtp_server_var.set(str(email_cfg.get("smtp_server", "")))
        self.cfg_smtp_port_var.set(str(email_cfg.get("smtp_port", 587)))
        self.cfg_smtp_tls_var.set(bool(email_cfg.get("use_starttls", True)))
        self.cfg_email_sender_var.set(str(email_cfg.get("sender", "")))
        self._has_saved_email_password = bool(str(email_cfg.get("password", "")).strip())
        self.cfg_email_password_var.set("")
        existing_receivers = email_cfg.get("recipients", [])
        if isinstance(existing_receivers, list):
            self.cfg_email_receivers_list = [str(x).strip() for x in existing_receivers if str(x).strip()]
            self.cfg_email_receivers_var.set(", ".join(self.cfg_email_receivers_list))
        else:
            self.cfg_email_receivers_list = []
            self.cfg_email_receivers_var.set("")
        self.cfg_email_receiver_input_var.set("")

        win = ctk.CTkToplevel(self.root)
        win.title("First-Time Setup" if force else "App Configuration")
        cfg_width, cfg_height = 900, 700
        self._center_toplevel(win, cfg_width, cfg_height)
        win.resizable(False, False)
        win.configure(fg_color=BG)
        win.transient(self.root)
        win.lift()
        win.focus_force()
        self.config_window = win

        if force:
            win.protocol("WM_DELETE_WINDOW", self.root.destroy)
            messagebox.showinfo(
                "Setup Required",
                "Please complete setup first.\n\n"
                "Required:\n"
                "- MESDP URL\n"
                "- MESDP Token\n"
                "- SMTP Server\n"
                "- SMTP Port\n"
                "- Email sender\n"
                "- Email password\n"
                "- Receiver email(s)"
            )

        card = ctk.CTkFrame(win, fg_color=SURFACE, corner_radius=12,
                            border_width=1, border_color=BORDER)
        card.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        # Inner frame keeps the content off the card edge.
        frame = ctk.CTkFrame(card, fg_color="transparent")
        frame.pack(fill=tk.BOTH, expand=True, padx=28, pady=(20, 22))

        ctk.CTkLabel(frame, text="FIRST-TIME SETUP" if force else "APP CONFIGURATION", text_color=TEXT,
                     font=(FONT_UI, 18, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(4, 2))
        ctk.CTkLabel(frame,
                     text="Configure MESDP and SMTP details to enable reminders.",
                     text_color=TEXT_MUTED, font=(FONT_UI, 12)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))

        lbl = dict(text_color=TEXT_MUTED, font=(FONT_UI, 12, "bold"))
        ent = dict(width=530, height=38, fg_color=SURFACE_ALT, text_color=TEXT,
                   border_color=BORDER, border_width=1, font=(FONT_UI, 12), corner_radius=8)

        fields = [
            ("MESDP Link", self.cfg_mesdp_url_var, False),
            ("MESDP Token", self.cfg_mesdp_token_var, True),
            ("SMTP Server", self.cfg_smtp_server_var, False),
            ("SMTP Port", self.cfg_smtp_port_var, False),
            ("Email Sender", self.cfg_email_sender_var, False),
            ("Email Password", self.cfg_email_password_var, True),
        ]

        entry_refs: dict[str, ctk.CTkEntry] = {}
        for i, (title, var, masked) in enumerate(fields, start=2):
            ctk.CTkLabel(frame, text=title, **lbl).grid(row=i, column=0, sticky="w", pady=(0, 8), padx=(0, 12))
            e = ctk.CTkEntry(frame, textvariable=var, **ent)
            if masked:
                e.configure(show="*")
            e.grid(row=i, column=1, sticky="w", pady=(0, 8))
            entry_refs[title] = e

        if self._has_saved_mesdp_token:
            entry_refs["MESDP Token"].configure(placeholder_text="Saved (leave blank to keep current token)")
        if self._has_saved_email_password:
            entry_refs["Email Password"].configure(placeholder_text="Saved (leave blank to keep current password)")

        receiver_row = len(fields) + 2
        ctk.CTkLabel(frame, text="Add Receiver Email", **lbl).grid(
            row=receiver_row, column=0, sticky="w", pady=(0, 8), padx=(0, 12)
        )
        receiver_wrap = ctk.CTkFrame(frame, fg_color="transparent")
        receiver_wrap.grid(row=receiver_row, column=1, sticky="w", pady=(0, 8))

        receiver_entry = ctk.CTkEntry(
            receiver_wrap,
            textvariable=self.cfg_email_receiver_input_var,
            width=350,
            height=38,
            fg_color=SURFACE_ALT,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            font=(FONT_UI, 12),
            corner_radius=8,
            placeholder_text="example@company.com"
        )
        receiver_entry.pack(side=tk.LEFT, padx=(0, 8))
        receiver_entry.bind("<Return>", lambda _e: self._add_receiver_email())

        ctk.CTkButton(
            receiver_wrap,
            text="ADD",
            width=70,
            height=38,
            fg_color=SURFACE_ALT,
            hover_color=BORDER_LT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            corner_radius=8,
            font=(FONT_UI, 11, "bold"),
            command=self._add_receiver_email,
        ).pack(side=tk.LEFT, padx=(0, 8))

        ctk.CTkButton(
            receiver_wrap,
            text="IMPORT LIST",
            width=110,
            height=38,
            fg_color=SURFACE_ALT,
            hover_color=BORDER_LT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            corner_radius=8,
            font=(FONT_UI, 10, "bold"),
            command=self._import_receiver_emails,
        ).pack(side=tk.LEFT)

        list_row = receiver_row + 1
        ctk.CTkLabel(frame, text="Configured Receiver(s)", **lbl).grid(
            row=list_row, column=0, sticky="nw", pady=(0, 8), padx=(0, 12)
        )
        list_wrap = ctk.CTkFrame(frame, fg_color="transparent")
        list_wrap.grid(row=list_row, column=1, sticky="w", pady=(0, 8))

        self.cfg_receiver_listbox = tk.Listbox(
            list_wrap,
            height=4,
            width=55,
            bg=SURFACE_ALT,
            fg=TEXT,
            selectbackground=ACCENT_HVR,
            selectforeground=ON_ACCENT,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            relief="flat",
            borderwidth=6,  # flat border in the background colour = inner padding
            activestyle="none",
            font=(FONT_UI, 11),
        )
        self.cfg_receiver_listbox.pack(side=tk.LEFT)

        ctk.CTkButton(
            list_wrap,
            text="REMOVE",
            width=90,
            height=32,
            fg_color=SURFACE_ALT,
            hover_color=BORDER_LT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            corner_radius=8,
            font=(FONT_UI, 10, "bold"),
            command=self._remove_selected_receiver_email,
        ).pack(side=tk.LEFT, padx=(8, 8), anchor="n")

        ctk.CTkButton(
            list_wrap,
            text="CLEAR ALL",
            width=90,
            height=32,
            fg_color=SURFACE_ALT,
            hover_color=BORDER_LT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            corner_radius=8,
            font=(FONT_UI, 10, "bold"),
            command=self._clear_all_receiver_emails,
        ).pack(side=tk.LEFT, anchor="n")

        self._refresh_receiver_listbox()

        tls_row = list_row + 1
        ctk.CTkLabel(frame, text="Use STARTTLS", **lbl).grid(
            row=tls_row, column=0, sticky="w", pady=(0, 8), padx=(0, 12)
        )
        ctk.CTkCheckBox(
            frame,
            text="Enable STARTTLS",
            variable=self.cfg_smtp_tls_var,
            text_color=TEXT,
            font=(FONT_UI, 12),
            checkbox_width=18,
            checkbox_height=18,
        ).grid(row=tls_row, column=1, sticky="w", pady=(0, 8))

        btn_wrap = ctk.CTkFrame(frame, fg_color="transparent")
        btn_wrap.grid(row=tls_row + 1, column=0, columnspan=2, sticky="w")
        ctk.CTkButton(
            btn_wrap,
            text="TEST CONNECTION",
            width=170,
            height=38,
            fg_color=SURFACE_ALT,
            hover_color=BORDER_LT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            corner_radius=8,
            font=(FONT_UI, 11, "bold"),
            command=self._test_runtime_config,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(
            btn_wrap,
            text="TEST SEND TO RECEIVER",
            width=190,
            height=38,
            fg_color=SURFACE_ALT,
            hover_color=BORDER_LT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            corner_radius=8,
            font=(FONT_UI, 11, "bold"),
            command=self._send_test_email_to_receivers,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(
            btn_wrap,
            text="SAVE CONFIG",
            width=150,
            height=38,
            fg_color=ACCENT,
            hover_color=ACCENT_HVR,
            text_color=ON_ACCENT,
            corner_radius=8,
            font=(FONT_UI, 11, "bold"),
            command=lambda: self._save_runtime_config(force=force),
        ).pack(side=tk.LEFT, padx=(0, 8))

        if force:
            ctk.CTkButton(
                btn_wrap,
                text="📂  RESTORE FROM BACKUP",
                width=200,
                height=38,
                fg_color=SURFACE_ALT,
                hover_color=BORDER_LT,
                text_color=TEXT,
                border_width=1,
                border_color=BORDER,
                corner_radius=8,
                font=(FONT_UI, 11, "bold"),
                command=lambda: self._restore_config_to_form(),
            ).pack(side=tk.LEFT, padx=(0, 8))

        if not force:
            btn_wrap2 = ctk.CTkFrame(frame, fg_color="transparent")
            btn_wrap2.grid(row=tls_row + 2, column=0, columnspan=2, sticky="w", pady=(6, 0))
            ctk.CTkButton(
                btn_wrap2,
                text="💾  BACKUP CONFIG",
                width=180,
                height=34,
                fg_color=SURFACE_ALT,
                hover_color=BORDER_LT,
                text_color=TEXT,
                border_width=1,
                border_color=BORDER,
                corner_radius=8,
                font=(FONT_UI, 11, "bold"),
                command=self._backup_config,
            ).pack(side=tk.LEFT, padx=(0, 8))
            ctk.CTkButton(
                btn_wrap2,
                text="📂  RESTORE CONFIG",
                width=180,
                height=34,
                fg_color=SURFACE_ALT,
                hover_color=BORDER_LT,
                text_color=TEXT,
                border_width=1,
                border_color=BORDER,
                corner_radius=8,
                font=(FONT_UI, 11, "bold"),
                command=self._restore_config,
            ).pack(side=tk.LEFT)

            # Recenter after widgets are laid out (handles DPI scaling correctly).
            win.after(30, lambda: self._center_existing_toplevel(win))

    def _refresh_receiver_listbox(self) -> None:
        if not self.cfg_receiver_listbox:
            return
        self.cfg_receiver_listbox.delete(0, tk.END)
        for email in self.cfg_email_receivers_list:
            self.cfg_receiver_listbox.insert(tk.END, email)
        self.cfg_email_receivers_var.set(", ".join(self.cfg_email_receivers_list))

    def _add_receiver_email(self) -> None:
        raw = self.cfg_email_receiver_input_var.get().strip()
        if not raw:
            return
        email_rx = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        candidates = [x.strip() for x in re.split(r"[,;\n]+", raw) if x.strip()]
        invalid = [x for x in candidates if not email_rx.match(x)]
        if invalid:
            messagebox.showerror("Invalid Receiver", f"Invalid email(s): {', '.join(invalid)}")
            return
        existing_lc = {e.lower() for e in self.cfg_email_receivers_list}
        for email in candidates:
            if email.lower() not in existing_lc:
                self.cfg_email_receivers_list.append(email)
                existing_lc.add(email.lower())
        self.cfg_email_receiver_input_var.set("")
        self._refresh_receiver_listbox()

    def _import_receiver_emails(self) -> None:
        self._add_receiver_email()

    def _clear_all_receiver_emails(self) -> None:
        if not self.cfg_email_receivers_list:
            return
        if messagebox.askyesno("Clear Receivers", "Remove all receiver emails from the list?"):
            self.cfg_email_receivers_list = []
            self._refresh_receiver_listbox()

    def _remove_selected_receiver_email(self) -> None:
        if not self.cfg_receiver_listbox:
            return
        sel = list(self.cfg_receiver_listbox.curselection())
        if not sel:
            return
        for idx in sorted(sel, reverse=True):
            if 0 <= idx < len(self.cfg_email_receivers_list):
                del self.cfg_email_receivers_list[idx]
        self._refresh_receiver_listbox()

    def _collect_runtime_config_inputs(self) -> tuple[dict, str | None]:
        base_url = self.cfg_mesdp_url_var.get().strip()
        token = self.cfg_mesdp_token_var.get().strip() or str(CONFIG.get("mesdp", {}).get("auth_token", "")).strip()
        smtp_server = self.cfg_smtp_server_var.get().strip()
        smtp_port_raw = self.cfg_smtp_port_var.get().strip()
        use_starttls = bool(self.cfg_smtp_tls_var.get())
        sender = self.cfg_email_sender_var.get().strip()
        password = self.cfg_email_password_var.get().strip() or str(CONFIG.get("email", {}).get("password", "")).strip()
        pending_input = self.cfg_email_receiver_input_var.get().strip()
        if pending_input:
            self._add_receiver_email()
        receivers = [x.strip() for x in self.cfg_email_receivers_list if x.strip()]

        if not (base_url and token and smtp_server and smtp_port_raw and sender and password and receivers):
            return {}, (
                "Please fill all required fields:\n"
                "MESDP Link, MESDP Token, SMTP Server, SMTP Port, Email Sender, "
                "Email Password, and Receiver(s)."
            )

        try:
            smtp_port = int(smtp_port_raw)
        except ValueError:
            return {}, "SMTP Port must be a valid integer (1-65535)."
        if not (1 <= smtp_port <= 65535):
            return {}, "SMTP Port must be between 1 and 65535."

        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {}, "MESDP Link must be a valid URL (http/https)."

        email_rx = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        if not email_rx.match(sender):
            return {}, "Email Sender is not a valid email address."
        bad = [e for e in receivers if not email_rx.match(e)]
        if bad:
            return {}, f"Invalid receiver email(s): {', '.join(bad)}"

        payload = {
            "mesdp": {
                "base_url": base_url.rstrip("/"),
                "auth_token": token,
            },
            "email": {
                "smtp_server": smtp_server,
                "smtp_port": smtp_port,
                "use_starttls": use_starttls,
                "sender": sender,
                "password": password,
                "recipients": receivers,
            },
        }
        return payload, None

    def _test_runtime_config(self) -> None:
        payload, err = self._collect_runtime_config_inputs()
        if err:
            messagebox.showerror("Invalid Input", err)
            return

        mesdp = payload["mesdp"]
        email = payload["email"]
        smtp_server = str(email.get("smtp_server", ""))
        smtp_port = int(email.get("smtp_port", 587))
        use_starttls = bool(email.get("use_starttls", True))

        code = (
            "import json, smtplib, ssl, requests\n"
            f"base={mesdp['base_url']!r}\n"
            f"token={mesdp['auth_token']!r}\n"
            f"sender={email['sender']!r}\n"
            f"password={email['password']!r}\n"
            f"smtp_server={smtp_server!r}\n"
            f"smtp_port={smtp_port}\n"
            f"use_starttls={use_starttls}\n"
            "out={'mesdp':'FAIL','smtp':'FAIL'}\n"
            "try:\n"
            "  h={'authtoken': token, 'Accept':'application/vnd.manageengine.sdp.v3+json'}\n"
            "  p={'input_data':'{\"list_info\":{\"start_index\":1,\"row_count\":1}}'}\n"
            "  r=requests.get(base + '/api/v3/requests', headers=h, params=p, timeout=12)\n"
            "  out['mesdp']='OK' if r.status_code < 400 else f'HTTP {r.status_code}'\n"
            "except Exception as e:\n"
            "  out['mesdp']=str(e)[:140]\n"
            "try:\n"
            "  s=smtplib.SMTP(smtp_server, smtp_port, timeout=12)\n"
            "  if use_starttls: s.starttls(context=ssl.create_default_context())\n"
            "  s.login(sender, password)\n"
            "  s.quit()\n"
            "  out['smtp']='OK'\n"
            "except Exception as e:\n"
            "  out['smtp']=str(e)[:140]\n"
            "print(json.dumps(out))\n"
        )
        rc, out = self._run_python_inline(code)
        if rc != 0:
            messagebox.showerror("Test Failed", out or "Unknown test error")
            return
        parsed = None
        for line in reversed(out.splitlines()):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(parsed, dict):
            messagebox.showerror("Test Failed", out or "No response from test runner")
            return

        mesdp_res = str(parsed.get("mesdp", "FAIL"))
        smtp_res = str(parsed.get("smtp", "FAIL"))
        ok = mesdp_res == "OK" and smtp_res == "OK"
        if ok:
            messagebox.showinfo("Test Connection", "MESDP: OK\nSMTP: OK")
        else:
            messagebox.showwarning("Test Connection", f"MESDP: {mesdp_res}\nSMTP: {smtp_res}")

    def _send_test_email_to_receivers(self) -> None:
        payload, err = self._collect_runtime_config_inputs()
        if err:
            messagebox.showerror("Invalid Input", err)
            return

        email = payload["email"]
        smtp_server = str(email.get("smtp_server", ""))
        smtp_port = int(email.get("smtp_port", 587))
        use_starttls = bool(email.get("use_starttls", True))
        recipients = [str(r).strip() for r in email.get("recipients", []) if str(r).strip()]
        if not recipients:
            messagebox.showerror("Test Email Failed", "No receiver email configured.")
            return

        code = (
            "import smtplib, ssl\n"
            "from email.mime.text import MIMEText\n"
            f"sender={email['sender']!r}\n"
            f"password={email['password']!r}\n"
            f"recipients={recipients!r}\n"
            f"smtp_server={smtp_server!r}\n"
            f"smtp_port={smtp_port}\n"
            f"use_starttls={use_starttls}\n"
            "msg=MIMEText('This is a test email from CLL MESDP Ticketing Worklog setup.')\n"
            "msg['Subject']='CLL MESDP Setup Test Email'\n"
            "msg['From']=sender\n"
            "msg['To']=', '.join(recipients)\n"
            "s=smtplib.SMTP(smtp_server, smtp_port, timeout=15)\n"
            "if use_starttls: s.starttls(context=ssl.create_default_context())\n"
            "s.login(sender, password)\n"
            "s.sendmail(sender, recipients, msg.as_string())\n"
            "s.quit()\n"
            "print('OK')\n"
        )
        rc, out = self._run_python_inline(code)
        if rc == 0 and "OK" in out:
            messagebox.showinfo("Test Email", f"Test email sent to: {', '.join(recipients)}")
        else:
            messagebox.showerror("Test Email Failed", out or "Failed to send test email.")

    def _restore_config_to_form(self) -> None:
        """Load a backup config file and auto-populate all form fields."""
        if not self.config_window or not self.config_window.winfo_exists():
            return
        
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.config_window,
            title="Restore Config from Backup",
            filetypes=[("JSON backup", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        
        ok, err, data = import_config_backup(path)
        if not ok:
            messagebox.showerror("Restore Failed", f"Could not read backup file:\n{err}", parent=self.config_window)
            return
        
        # Auto-populate form fields
        mesdp_cfg = data.get("mesdp", {}) if isinstance(data, dict) else {}
        email_cfg = data.get("email", {}) if isinstance(data, dict) else {}
        
        # MESDP fields
        self.cfg_mesdp_url_var.set(str(mesdp_cfg.get("base_url", "")))
        self.cfg_mesdp_token_var.set(str(mesdp_cfg.get("auth_token", "")))
        self._has_saved_mesdp_token = bool(str(mesdp_cfg.get("auth_token", "")).strip())
        
        # Email fields
        self.cfg_smtp_server_var.set(str(email_cfg.get("smtp_server", "")))
        self.cfg_smtp_port_var.set(str(email_cfg.get("smtp_port", 587)))
        self.cfg_smtp_tls_var.set(bool(email_cfg.get("use_starttls", True)))
        self.cfg_email_sender_var.set(str(email_cfg.get("sender", "")))
        self.cfg_email_password_var.set(str(email_cfg.get("password", "")))
        self._has_saved_email_password = bool(str(email_cfg.get("password", "")).strip())
        
        # Receivers
        existing_receivers = email_cfg.get("recipients", [])
        if isinstance(existing_receivers, list):
            self.cfg_email_receivers_list = [str(x).strip() for x in existing_receivers if str(x).strip()]
        else:
            self.cfg_email_receivers_list = []
        self._refresh_receiver_listbox()
        
        messagebox.showinfo("Config Restored", "Configuration loaded from backup.\nFields populated. Review and save when ready.", parent=self.config_window)

    def _save_runtime_config(self, force: bool = False) -> None:
        payload, err = self._collect_runtime_config_inputs()
        if err:
            messagebox.showerror("Invalid Input", err)
            return

        if not save_user_config(payload):
            messagebox.showerror("Save Failed", "Unable to write config file.")
            return

        if self.config_window and self.config_window.winfo_exists():
            self.config_window.destroy()
            self.config_window = None

        self._set_output(f"Config saved. Receivers: {', '.join(payload['email']['recipients'])}")
        if force and self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.setup_required = False
        if force or runtime_config_ready(CONFIG):
            self.refresh_status()

    def _backup_config(self) -> None:
        from tkinter import filedialog
        parent = self.config_window if (self.config_window and self.config_window.winfo_exists()) else self.root
        path = filedialog.asksaveasfilename(
            parent=parent,
            title="Save Config Backup",
            defaultextension=".json",
            filetypes=[("JSON backup", "*.json"), ("All files", "*.*")],
            initialfile="mesdp_config_backup.json",
        )
        if not path:
            return
        if export_config_backup(path):
            messagebox.showinfo("Backup Saved", f"Config backed up to:\n{path}", parent=parent)
        else:
            messagebox.showerror("Backup Failed", "No config file found to back up.\nSave your config first.", parent=parent)

    def _restore_config(self) -> None:
        from tkinter import filedialog
        parent = self.config_window if (self.config_window and self.config_window.winfo_exists()) else self.root
        path = filedialog.askopenfilename(
            parent=parent,
            title="Restore Config from Backup",
            filetypes=[("JSON backup", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        ok, err, data = import_config_backup(path)
        if not ok:
            messagebox.showerror("Restore Failed", f"Could not read backup file:\n{err}", parent=parent)
            return
        if not save_user_config(data):
            messagebox.showerror("Restore Failed", "Could not write config file.", parent=parent)
            return
        messagebox.showinfo("Restore Complete", "Configuration restored successfully.\nThe config window will reload.", parent=parent)
        if self.config_window and self.config_window.winfo_exists():
            self.config_window.destroy()
            self.config_window = None
        self._open_runtime_config_window(force=False)

    def open_shift_settings_window(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift(); self.settings_window.focus_force(); return

        win = ctk.CTkToplevel(self.root)
        win.title("Shift Settings  (Ctrl+,)")
        settings_width, settings_height = 900, 640
        self._center_toplevel(win, settings_width, settings_height)
        win.resizable(False, False)
        win.configure(fg_color=BG)
        win.transient(self.root)
        win.lift()
        win.focus_force()
        self.settings_window = win

        card = ctk.CTkFrame(win, fg_color=SURFACE, corner_radius=12,
                            border_width=1, border_color=BORDER)
        card.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        # Inner frame keeps the content off the card edge.
        c = ctk.CTkFrame(card, fg_color="transparent")
        c.pack(fill=tk.BOTH, expand=True, padx=28, pady=(20, 22))
        c.grid_columnconfigure(0, minsize=190)
        c.grid_columnconfigure(1, minsize=125)
        c.grid_columnconfigure(2, minsize=125)
        c.grid_columnconfigure(3, minsize=180)

        ctk.CTkLabel(c, text="SHIFT SETTINGS", text_color=TEXT,
                     font=(FONT_UI, 18, "bold")).grid(
            row=0, column=0, columnspan=7, sticky="w", pady=(4, 2))
        ctk.CTkLabel(c,
                     text="Adjust shift windows and auto-refresh behavior.",
                     text_color=TEXT_MUTED, font=(FONT_UI, 12)).grid(
            row=1, column=0, columnspan=7, sticky="w", pady=(0, 12))

        lbl_cfg = dict(text_color=TEXT_MUTED, font=(FONT_UI, 14, "bold"))
        for col, text in enumerate(["Shift", "Start (HH:MM)", "End (HH:MM)",
                                    "Reminders (min before end)"]):
            ctk.CTkLabel(c, text=text, **lbl_cfg).grid(
                row=2, column=col, padx=(0, 10), pady=(0, 6), sticky="w")

        self.shift_hour_vars     = {}
        self.shift_reminder_vars = {}
        entry_cfg = dict(width=104, height=36, fg_color=SURFACE_ALT, text_color=TEXT,
                         border_color=BORDER, border_width=1, corner_radius=8, font=(FONT_MONO, 14))

        for ri, sn in enumerate(SHIFT_NAMES, start=3):
            ctk.CTkLabel(c, text=sn.capitalize(), text_color=TEXT,
                         font=(FONT_UI, 14, "bold")).grid(
                row=ri, column=0, padx=(0, 10), pady=(0, 8), sticky="w")
            sv, ev, rv = tk.StringVar(), tk.StringVar(), tk.StringVar(value="60, 30")
            for ci, var in enumerate([sv, ev, rv], start=1):
                e = ctk.CTkEntry(c, textvariable=var, **entry_cfg)
                e.grid(row=ri, column=ci, padx=(0, 10), pady=(0, 8), sticky="w")
                e.bind("<KeyRelease>", lambda _e: self._refresh_shift_preview())
            self.shift_hour_vars[sn]     = (sv, ev)
            self.shift_reminder_vars[sn] = rv

        btn_row = 6
        ghost_btn = dict(border_width=1, border_color=BORDER, font=(FONT_UI, 14, "bold"))

        btn_frame = ctk.CTkFrame(c, fg_color="transparent")
        btn_frame.grid(row=btn_row, column=0, columnspan=7, pady=(4, 0), sticky="w")
        for txt, fg, hvr, cmd in [
            ("SAVE",          ACCENT,      ACCENT_HVR, self.save_shift_hours),
            ("RESET DEFAULT", SURFACE_ALT, BORDER_LT,  self.reset_settings_to_default),
            ("CLOSE",         SURFACE_ALT, BORDER_LT,  win.destroy),
        ]:
            ctk.CTkButton(btn_frame, text=txt, width=160, height=38,
                          fg_color=fg, hover_color=hvr,
                          text_color=ON_ACCENT if fg == ACCENT else TEXT, corner_radius=8,
                          command=cmd, **ghost_btn).pack(side=tk.LEFT, padx=(0, 8))

        ctk.CTkLabel(c, textvariable=self.shift_preview_var,
                     text_color=TEXT_DIM, font=(FONT_MONO, 14)).grid(
            row=btn_row + 1, column=0, columnspan=7, sticky="w", pady=(8, 0))

        sep = ctk.CTkFrame(c, fg_color=BORDER, height=1)
        sep.grid(row=btn_row + 2, column=0, columnspan=7, sticky="ew", pady=(14, 10))

        ctk.CTkLabel(c, text="AUTO REFRESH", text_color=TEXT_MUTED,
                     font=(FONT_MONO, 14, "bold")).grid(
            row=btn_row + 3, column=0, columnspan=7, sticky="w", pady=(0, 8))

        self.settings_auto_refresh_check = ctk.CTkCheckBox(
            c, text="Enable auto refresh", variable=self.auto_refresh_var,
            text_color=TEXT, font=(FONT_UI, 14),
            checkbox_width=22, checkbox_height=22,
            command=self._toggle_auto_refresh)
        self.settings_auto_refresh_check.grid(
            row=btn_row + 4, column=0, columnspan=3, padx=(0, 16), pady=(0, 8), sticky="w")

        ctk.CTkLabel(c, text="Profile", **lbl_cfg).grid(
            row=btn_row + 5, column=0, padx=(0, 8), pady=(0, 8), sticky="w")
        self.refresh_profile_combo = ctk.CTkComboBox(
            c, values=["Realtime", "Balanced", "Low API Load", "Custom"],
            width=200, height=34, state="readonly", font=(FONT_UI, 14), dropdown_font=(FONT_UI, 14),
            command=lambda _v: self._on_profile_changed())
        self.refresh_profile_combo.grid(row=btn_row + 5, column=1, columnspan=2, padx=(0, 16), pady=(0, 8), sticky="w")
        self.refresh_profile_combo.set(self.refresh_profile_var.get())

        ctk.CTkLabel(c, text="Interval", **lbl_cfg).grid(
            row=btn_row + 6, column=0, padx=(0, 8), pady=(0, 8), sticky="w")
        self.refresh_interval_combo = ctk.CTkComboBox(
            c, values=["10", "15", "30", "45", "60", "120", "300", "600"],
            width=104, height=34, font=(FONT_UI, 14), dropdown_font=(FONT_UI, 14),
            command=lambda _v: self._set_profile_custom())
        self.refresh_interval_combo.grid(row=btn_row + 6, column=1, padx=(0, 8), pady=(0, 8), sticky="w")
        self.refresh_interval_combo.set(self.refresh_interval_var.get())

        self.refresh_unit_combo = ctk.CTkComboBox(
            c, values=["Seconds", "Minutes", "Hours"],
            width=138, height=34, state="readonly", font=(FONT_UI, 14), dropdown_font=(FONT_UI, 14),
            command=lambda _v: self._set_profile_custom())
        self.refresh_unit_combo.grid(row=btn_row + 6, column=2, padx=(0, 16), pady=(0, 8), sticky="w")
        self.refresh_unit_combo.set(self.refresh_interval_unit_var.get())

        ctk.CTkButton(c, text="SAVE AUTO REFRESH", width=180, height=36,
                      fg_color=ACCENT, hover_color=ACCENT_HVR, text_color=ON_ACCENT,
                      corner_radius=8, font=(FONT_UI, 14, "bold"),
                      command=self.save_auto_refresh_only).grid(
            row=btn_row + 7, column=0, padx=(0, 16), pady=(8, 0), sticky="w")

        self._load_shift_hours_into_form()
        self._apply_profile_to_interval(set_custom_if_manual=False)
        self._apply_auto_refresh_info_label()

        # Recenter after widgets are laid out (handles DPI scaling correctly).
        win.after(30, lambda: self._center_existing_toplevel(win))

    def _set_profile_custom(self) -> None:
        self.refresh_profile_var.set("Custom")
        if hasattr(self, "refresh_profile_combo"):
            self.refresh_profile_combo.set("Custom")

    def _on_profile_changed(self, _event=None) -> None:
        self._sync_refresh_combo_to_vars()
        self._apply_profile_to_interval(set_custom_if_manual=False)

    def _apply_profile_to_interval(self, set_custom_if_manual: bool = True) -> None:
        profile = self.refresh_profile_var.get()
        if profile in self.REFRESH_PROFILES:
            self._set_interval_fields_from_seconds(self.REFRESH_PROFILES[profile])
            for attr, getter in [("refresh_interval_combo", lambda: self.refresh_interval_var.get()),
                                  ("refresh_unit_combo",     lambda: self.refresh_interval_unit_var.get()),
                                  ("refresh_profile_combo",  lambda: profile)]:
                if hasattr(self, attr):
                    getattr(self, attr).set(getter())
            return
        if set_custom_if_manual:
            self.refresh_profile_var.set("Custom")
            if hasattr(self, "refresh_profile_combo"):
                self.refresh_profile_combo.set("Custom")

    @staticmethod
    def _parse_reminder_offsets(text: str) -> list[int] | None:
        """'60, 30' -> [60, 30] (largest first); None if any value is invalid."""
        parts = [p for p in re.split(r"[,\s;]+", str(text).strip()) if p]
        try:
            values = sorted({int(p) for p in parts}, reverse=True)
        except ValueError:
            return None
        if not values or any(not 0 <= v <= 720 for v in values):
            return None
        return values

    def _refresh_shift_preview(self, reminders: dict | None = None) -> None:
        if reminders is None:
            reminders = {}
            for sn, (_, ev) in self.shift_hour_vars.items():
                try:
                    hh, mm  = ev.get().strip().split(":")
                    end_min = int(hh) * 60 + int(mm)
                    offsets = self._parse_reminder_offsets(self.shift_reminder_vars[sn].get())
                    if not offsets:
                        raise ValueError
                    times = []
                    for offset in offsets:
                        rem = (end_min - offset) % (24 * 60)
                        times.append(f"{rem // 60:02d}:{rem % 60:02d}")
                    reminders[sn] = times
                except (ValueError, AttributeError, KeyError):
                    reminders[sn] = ["--:--"]

        def fmt(v) -> str:
            return " & ".join(v) if isinstance(v, (list, tuple)) else str(v)
        self.shift_preview_var.set(
            f"Preview  ·  Morning {fmt(reminders.get('morning', '--:--'))}  "
            f"·  Night {fmt(reminders.get('night', '--:--'))}")

    def save_shift_hours(self) -> None:
        self._sync_refresh_combo_to_vars()
        self._apply_profile_to_interval(set_custom_if_manual=False)
        try:
            interval_seconds = self._interval_seconds_from_fields()
        except ValueError:
            messagebox.showerror("Invalid Input", "Refresh interval must be an integer."); return
        if not (5 <= interval_seconds <= 86400):
            messagebox.showerror("Invalid Input", "Interval must be between 5s and 24h."); return

        self.auto_refresh_ms = interval_seconds * 1000
        if (self.refresh_profile_var.get() in self.REFRESH_PROFILES and
                self.REFRESH_PROFILES[self.refresh_profile_var.get()] != interval_seconds):
            self.refresh_profile_var.set("Custom")
            if hasattr(self, "refresh_profile_combo"):
                self.refresh_profile_combo.set("Custom")
        self._save_gui_preferences()

        new_hours: dict[str, dict] = {}
        for sn, (sv, ev) in self.shift_hour_vars.items():
            try:
                sh, sm = sv.get().strip().split(":")
                eh, em = ev.get().strip().split(":")
                sh, sm, eh, em = int(sh), int(sm), int(eh), int(em)
            except ValueError:
                messagebox.showerror("Invalid Input",
                    f"{sn.capitalize()}: use HH:MM format."); return
            if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
                messagebox.showerror("Invalid Time",
                    f"{sn.capitalize()}: time out of valid range."); return
            rm = self._parse_reminder_offsets(self.shift_reminder_vars[sn].get())
            if not rm:
                messagebox.showerror("Invalid Reminder",
                    f"{sn.capitalize()}: reminders must be minutes before shift end (0–720), "
                    f"separated by commas, e.g. 60, 30."); return
            new_hours[sn] = {"start_time": f"{sh:02d}:{sm:02d}",
                              "end_time":   f"{eh:02d}:{em:02d}",
                              "reminder_minutes": rm}
        try:
            os.makedirs(os.path.dirname(self.shift_settings_file), exist_ok=True)
            with open(self.shift_settings_file, "w", encoding="utf-8") as f:
                json.dump({"shift_hours": new_hours}, f, indent=2)
        except OSError as e:
            messagebox.showerror("Save Failed", str(e)); return

        self._refresh_shift_preview()
        self._apply_auto_refresh_info_label()
        self._schedule_next_auto_refresh()
        if messagebox.askyesno("Saved", "Settings saved.\n\nRestart scheduler to apply?"):
            self._execute_action(self.restart_script, "Restart")
        else:
            self.refresh_status()

    def reset_settings_to_default(self) -> None:
        for sn, (s, e, r) in {"morning": ("08:00","20:00","60, 30"),
                               "night":   ("20:00","08:00","60, 30")}.items():
            if sn in self.shift_hour_vars:
                self.shift_hour_vars[sn][0].set(s)
                self.shift_hour_vars[sn][1].set(e)
            if sn in self.shift_reminder_vars:
                self.shift_reminder_vars[sn].set(r)
        self.auto_refresh_var.set(True)
        self.refresh_profile_var.set("Balanced")
        self.refresh_interval_var.set("30")
        self.refresh_interval_unit_var.set("Seconds")
        self.auto_refresh_ms = 30000
        for attr, val in [("refresh_profile_combo","Balanced"),
                           ("refresh_interval_combo","30"),
                           ("refresh_unit_combo","Seconds")]:
            if hasattr(self, attr):
                getattr(self, attr).set(val)
        self._refresh_shift_preview()
        self._apply_auto_refresh_info_label()
        self._save_gui_preferences()

    def save_auto_refresh_only(self) -> None:
        self._sync_refresh_combo_to_vars()
        self._apply_profile_to_interval(set_custom_if_manual=False)
        try:
            interval_seconds = self._interval_seconds_from_fields()
        except ValueError:
            messagebox.showerror("Invalid Input", "Refresh interval must be an integer."); return
        if not (5 <= interval_seconds <= 86400):
            messagebox.showerror("Invalid Input", "Interval must be between 5s and 24h."); return

        self.auto_refresh_ms = interval_seconds * 1000
        if (self.refresh_profile_var.get() in self.REFRESH_PROFILES and
                self.REFRESH_PROFILES[self.refresh_profile_var.get()] != interval_seconds):
            self.refresh_profile_var.set("Custom")
            if hasattr(self, "refresh_profile_combo"):
                self.refresh_profile_combo.set("Custom")
        self._save_gui_preferences()
        self._apply_auto_refresh_info_label()
        self._schedule_next_auto_refresh()
        messagebox.showinfo("Saved", "Auto refresh settings saved.")

    # ── Scheduler state ───────────────────────────────────────────────────────

    def _parse_scheduler_state(self, output: str) -> tuple[str, str]:
        t = output.upper()
        if "NOT RUNNING" in t: return "NOT RUNNING", "Scheduler process is stopped."
        if "RUNNING"     in t: return "RUNNING",     "Scheduler is active and waiting for reminder times."
        return "UNKNOWN", "Unable to determine scheduler state."

    # Ticket data pulls one worklog call per In Progress ticket, so it is slow
    # (about a minute) and is refreshed less often than the scheduler status.
    DATA_TIMEOUT_SEC = 240
    DATA_MIN_INTERVAL_SEC = 300

    def _fetch_dashboard_data(self) -> dict:
        code = (
            "import json, sys\n"
            f"sys.path.insert(0, {self.project_root!r})\n"
            "import config as cfg\n"
            "cfg.reload_config_from_disk()\n"
            "import worklog_reminder as wr\n"
            "shift = wr.get_auto_shift()\n"
            "start, _ = wr._shift_window(shift)\n"
            "cutoff = wr.worklog_cutoff(shift)\n"
            "summary = wr.get_inprogress_summary(cutoff)\n"
            "activity = wr.get_shift_activity(shift) if summary is not None else None\n"
            "try:\n"
            "    roster = wr.get_shift_roster(shift)\n"
            "except Exception:\n"
            "    roster = wr._ROSTER_UNAVAILABLE\n"
            "def _r(x):\n"
            "    if not isinstance(x, dict):\n"
            "        return None\n"
            "    return {'shift': x.get('shift'), 'date': str(x.get('date') or ''),\n"
            "            'people': [{'name': p.get('name'), 'tier': p.get('tier'),\n"
            "                        'first_contact': bool(p.get('first_contact'))} for p in x.get('people', [])]}\n"
            "if isinstance(roster, dict):\n"
            "    ro = {'current': _r(roster.get('current')), 'next': _r(roster.get('next'))}\n"
            "else:\n"
            "    ro = None if roster is None else {'unavailable': True}\n"
            "print(json.dumps({\n"
            "    'shift': shift, 'shift_start': start.strftime('%H:%M'), 'cutoff': cutoff.strftime('%H:%M'),\n"
            "    'summary': None if summary is None else {\n"
            "        'pending': summary['pending_count'], 'updated': summary['updated_today_count'],\n"
            "        'total': summary['total_in_progress'], 'tickets': summary['tickets']},\n"
            "    'activity': None if not activity else {k: activity.get(k) for k in ('opened', 'closed', 'in_progress', 'on_hold')},\n"
            "    'roster': ro}))\n"
        )
        rc, out = self._run_python_inline(code, timeout_sec=self.DATA_TIMEOUT_SEC)
        for line in reversed((out or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"error": out or f"Data pull failed (exit {rc})."}

    def _update_ticket_table(self, tickets: list[dict]) -> None:
        self.latest_tickets = tickets
        self._refresh_filter_options(tickets)
        self._resize_ticket_columns()
        self._render_filtered_ticket_rows()

    def _apply_status_payload(self, mon_code: int, mon_out: str, scheduler_state: tuple[str, str]) -> None:
        state, msg = scheduler_state
        self._set_output(mon_out or "No monitor output.")
        if mon_code != 0:
            self.current_scheduler_state = "UNKNOWN"
            self._show_scheduler_state("ERROR")
            self.scheduler_detail.set("Could not read scheduler status. Open Log for details.")
        else:
            self.current_scheduler_state = state
            self._show_scheduler_state(state)
            self.scheduler_detail.set(
                "Scheduler is running and will send the reminders above." if state == "RUNNING"
                else "Scheduler is stopped. Press Start scheduler to send reminders." if state == "NOT RUNNING"
                else msg)
        self._set_start_stop_button_for_state(self.current_scheduler_state)

    def _apply_dashboard_data(self, data: dict) -> None:
        self._data_loading = False
        self.last_data_at = time.time()
        stamp = datetime.now().strftime("%H:%M")

        if "error" in data:
            self._set_kpis({})
            self._render_roster("error")
            self.data_updated_var.set(f"Last attempt {stamp}")
            self.table_empty_var.set("Could not reach MESDP. Check the network / VPN, then press Refresh.")
            self._update_ticket_table([])
            self.table_count_var.set("")
            self._set_output(f"{self.latest_monitor_output}\n\n[dashboard data]\n{data['error']}")
            return

        self._render_roster(data.get("roster"))
        shift = str(data.get("shift") or "")
        self.roster_note_var.set("Red name in roster = first contact")

        summary = data.get("summary")
        activity = data.get("activity") or {}
        since = data.get("shift_start", "")
        cutoff = data.get("cutoff", "")
        tickets = (summary or {}).get("tickets", [])
        today = datetime.now().date()

        def age(t: dict) -> int | None:
            try:
                return (today - datetime.strptime(str(t.get("last_worklog")), "%Y-%m-%d").date()).days
            except ValueError:
                return None

        values = {
            "in_progress": activity.get("in_progress"),
            "on_hold": activity.get("on_hold"),
            "opened": activity.get("opened"),
            "closed": activity.get("closed"),
        }
        if summary is not None:
            values.update({
                "pending": summary.get("pending"),
                "no_record": sum(1 for t in tickets if age(t) is None),
                "stale": sum(1 for t in tickets if (age(t) or 0) >= 3),
            })
        self._set_kpis(values, {
            "pending": f"No worklog since {cutoff}" if cutoff else "No update this shift",
            "opened": f"Since {since}" if since else "This shift",
            "closed": f"Since {since}" if since else "This shift",
        })

        if summary is None:
            self.table_empty_var.set("Could not reach MESDP. Check the network / VPN, then press Refresh.")
            self.data_updated_var.set(f"Last attempt {stamp}")
        else:
            self.table_empty_var.set(
                f"All In Progress tickets have a worklog since {cutoff}." if cutoff
                else "All In Progress tickets are updated.")
            self.data_updated_var.set(f"Updated {stamp}  ·  PM excluded  ·")
        self._update_ticket_table(tickets)
        if summary is None:
            self.table_count_var.set("")

    def _set_output(self, text: str) -> None:
        self.latest_monitor_output = text
        if self.output and self.output.winfo_exists():
            self.output.configure(state=tk.NORMAL)
            self.output.delete("1.0", tk.END)
            self.output.insert("1.0", text)
            self.output.configure(state=tk.DISABLED)

    def _show_close_options_dialog(self) -> str:
        result = tk.StringVar(value="close")

        dlg = ctk.CTkToplevel(self.root)
        dlg.title("Exit Options")
        dlg.geometry("420x190")
        dlg.resizable(False, False)
        dlg.configure(fg_color=SURFACE)
        dlg.transient(self.root)
        dlg.grab_set()

        c = ctk.CTkFrame(dlg, fg_color=SURFACE)
        c.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            c,
            text="Choose app action",
            text_color=TEXT,
            font=(FONT_UI, 16, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        ctk.CTkLabel(
            c,
            text="Run in background keeps scheduler process and app runtime active.",
            text_color=TEXT_MUTED,
            font=(FONT_UI, 12),
            wraplength=380,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 12))

        btn_cfg = dict(width=122, height=34, font=(FONT_UI, 10, "bold"))

        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(anchor="w")

        ctk.CTkButton(
            row,
            text="RUN IN BACKGROUND",
            fg_color=SURFACE_ALT,
            hover_color=BORDER_LT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            command=lambda: (result.set("background"), dlg.destroy()),
            **btn_cfg,
        ).pack(side=tk.LEFT, padx=(0, 8))

        ctk.CTkButton(
            row,
            text="RESTART APP",
            fg_color=SURFACE_ALT,
            hover_color=BORDER_LT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            command=lambda: (result.set("restart"), dlg.destroy()),
            **btn_cfg,
        ).pack(side=tk.LEFT, padx=(0, 8))

        ctk.CTkButton(
            row,
            text="CLOSE APP",
            fg_color=BAD_DIM,
            hover_color="#fee4e2",
            text_color=BAD,
            border_width=1,
            border_color=BAD,
            command=lambda: (result.set("close"), dlg.destroy()),
            **btn_cfg,
        ).pack(side=tk.LEFT)

        dlg.protocol("WM_DELETE_WINDOW", lambda: (result.set("close"), dlg.destroy()))
        dlg.wait_window()
        return result.get()

    def _notify_user(self, title: str, message: str) -> None:
        if self.tray_icon:
            try:
                self.tray_icon.notify(message, title)
                return
            except Exception:
                pass
        messagebox.showinfo(title, message)

    def _find_asset(self, filename: str) -> str | None:
        """Return absolute path to a bundled asset, works both frozen and dev."""
        candidates = []
        if getattr(sys, "frozen", False):
            candidates.append(os.path.join(sys._MEIPASS, filename))
        candidates.append(os.path.join(self.project_root, filename))
        candidates.append(os.path.join(self.project_root, "dist", filename))
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    def _set_window_icon(self) -> None:
        """Apply the app icon to the Tk window (titlebar + taskbar)."""
        try:
            ico = self._find_asset("app_icon.ico")
            if ico:
                self.root.iconbitmap(ico)
                return
        except Exception:
            pass
        try:
            if Image is not None:
                png = self._find_asset("app_icon.png")
                if png:
                    from PIL import ImageTk
                    img = Image.open(png)
                    photo = ImageTk.PhotoImage(img)
                    self.root.wm_iconphoto(True, photo)
                    self._icon_photo_ref = photo  # keep reference
        except Exception:
            pass

    def _create_tray_image(self):
        if Image is None:
            return None
        png = self._find_asset("app_icon.png")
        if png:
            try:
                img = Image.open(png).convert("RGBA")
                return img.resize((64, 64), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.ANTIALIAS)
            except Exception:
                pass
        # Fallback generic icon
        if ImageDraw is None:
            return None
        image = Image.new("RGB", (64, 64), "#0d1521")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 8, 56, 56), radius=10, fill="#2f80ed")
        draw.rectangle((18, 20, 46, 44), fill="#ffffff")
        draw.rectangle((22, 24, 42, 40), fill="#0d1521")
        return image

    def _open_dashboard_from_tray(self) -> None:
        self._stop_tray_icon()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _exit_from_tray(self) -> None:
        self._stop_tray_icon()
        self.root.destroy()

    def _restart_from_tray(self) -> None:
        self._stop_tray_icon()
        self._restart_app()

    def _start_tray_icon(self) -> bool:
        if pystray is None:
            return False
        if self.tray_icon is not None:
            return True

        image = self._create_tray_image()
        if image is None:
            return False

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", lambda _icon, _item: self.root.after(0, self._open_dashboard_from_tray)),
            pystray.MenuItem("Restart", lambda _icon, _item: self.root.after(0, self._restart_from_tray)),
            pystray.MenuItem("Exit", lambda _icon, _item: self.root.after(0, self._exit_from_tray)),
        )

        self.tray_icon = pystray.Icon("mesdp_worklog", image, "CLL MESDP Ticketing Worklog", menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()
        return True

    def _stop_tray_icon(self) -> None:
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.tray_thread = None

    def _restart_app(self) -> None:
        try:
            self._stop_tray_icon()
            if getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable], cwd=self.project_root)
            else:
                subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=self.project_root)
            self.root.destroy()
        except Exception as exc:
            messagebox.showerror("Restart Failed", str(exc))

    def _on_main_window_close(self) -> None:
        action = self._show_close_options_dialog()
        if action == "background":
            if not self._start_tray_icon():
                messagebox.showwarning(
                    "Tray Not Available",
                    "System tray is not available in this runtime. App will stay open."
                )
                return
            self.root.withdraw()
            self._set_output("App is running in background. Reopen from app shortcut to restore UI.")
            self._notify_user("Running In Background", "CLL MESDP Ticketing Worklog is now running in background.")
            return
        if action == "restart":
            self._notify_user("Restart App", "Restarting application now.")
            self._restart_app()
            return
        self._notify_user("Close App", "Application will close now.")
        self._stop_tray_icon()
        self.root.destroy()

    def _execute_action(self, script_path: str, action_name: str) -> None:
        def worker() -> None:
            self.root.after(0, lambda: self._set_buttons_state(tk.DISABLED))
            try:
                _code, out = self._run_ps(script_path)
                self.root.after(0, lambda: self._set_output(out or f"No output for {action_name}."))
            finally:
                # Always recover UI state even if script execution fails.
                self.root.after(0, lambda: self._set_buttons_state(tk.NORMAL))
                self.root.after(0, self.refresh_status)
        threading.Thread(target=worker, daemon=True).start()

    def _set_buttons_state(self, state: str) -> None:
        for btn in [self.start_stop_btn,
                    self.refresh_btn, self.run_now_btn, self.settings_btn, self.app_config_btn,
                    self.terminal_btn]:
            btn.configure(state=state)

    def run_one_time_report(self) -> None:
        if not runtime_config_ready(CONFIG):
            messagebox.showerror("Config Required", "Please complete CONFIG before running one-time report.")
            return
        if not messagebox.askyesno(
            "Run One-Time Report",
            "Send reminder report now using current shift detection?"
        ):
            return

        def worker() -> None:
            self._set_buttons_state(tk.DISABLED)
            self._set_output("Running one-time report now...")
            code = (
                "import json, sys\n"
                f"sys.path.insert(0, {self.project_root!r})\n"
                "import worklog_reminder as wr\n"
                "shift = wr.get_auto_shift()\n"
                "wr.execute_reminder(shift)\n"
                "print(json.dumps({'ok': True, 'shift': shift}))\n"
            )
            rc, out = self._run_python_inline(code)
            self._set_output(out or "No output from one-time report.")

            if rc != 0:
                self.root.after(0, lambda: messagebox.showerror("Run Failed", out or "Failed to run one-time report."))
            else:
                shift_name = "auto"
                for line in reversed((out or "").splitlines()):
                    try:
                        payload = json.loads(line)
                        if isinstance(payload, dict) and payload.get("ok"):
                            shift_name = str(payload.get("shift", "auto")).upper()
                            break
                    except json.JSONDecodeError:
                        continue
                self.root.after(0, lambda: messagebox.showinfo("Run Complete", f"One-time report sent for shift: {shift_name}"))

            self.root.after(0, lambda: self.refresh_status(force_data=True))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_auto_refresh_info_label(self) -> None:
        self._update_auto_refresh_countdown_label()

    def _update_auto_refresh_countdown_label(self) -> None:
        if self.auto_refresh_countdown_job:
            self.root.after_cancel(self.auto_refresh_countdown_job)
            self.auto_refresh_countdown_job = None

        if not self.auto_refresh_var.get():
            self.auto_refresh_info_var.set("Auto refresh: off")
            self.auto_refresh_count_var.set("")
            return

        interval_s = max(1, int(self.auto_refresh_ms / 1000))
        if self.next_auto_refresh_at is None:
            remaining = interval_s
        else:
            remaining = max(0, int(round(self.next_auto_refresh_at - time.time())))

        hh = remaining // 3600
        mm = (remaining % 3600) // 60
        ss = remaining % 60
        countdown = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh > 0 else f"{mm:02d}:{ss:02d}"

        if interval_s % 3600 == 0:
            unit_text = f"{interval_s // 3600} hour{'s' if interval_s // 3600 != 1 else ''}"
        elif interval_s % 60 == 0:
            unit_text = f"{interval_s // 60} min{'s' if interval_s // 60 != 1 else ''}"
        else:
            unit_text = f"{interval_s} sec{'s' if interval_s != 1 else ''}"

        self.auto_refresh_info_var.set(f"Auto refresh every {unit_text}")
        self.auto_refresh_count_var.set(
            "Loading ticket data…" if self._data_loading else f"Next status check in {countdown}")
        self.auto_refresh_countdown_job = self.root.after(1000, self._update_auto_refresh_countdown_label)

    def _set_start_stop_button_for_state(self, state: str) -> None:
        if state == "RUNNING":
            self.start_stop_btn.configure(
                text="■  Stop scheduler", fg_color=BAD, hover_color=BAD_HVR, text_color=ON_NAVY)
        else:
            self.start_stop_btn.configure(
                text="▶  Start scheduler", fg_color=GOOD, hover_color=GOOD_HVR, text_color=ON_NAVY)

    def _schedule_next_auto_refresh(self) -> None:
        if self.auto_refresh_job:
            self.root.after_cancel(self.auto_refresh_job)
            self.auto_refresh_job = None
        if self.auto_refresh_var.get():
            self.next_auto_refresh_at = time.time() + (self.auto_refresh_ms / 1000)
            self._update_auto_refresh_countdown_label()
            self.auto_refresh_job = self.root.after(self.auto_refresh_ms, self.refresh_status)
        else:
            self.next_auto_refresh_at = None
            self._update_auto_refresh_countdown_label()

    def _toggle_auto_refresh(self) -> None:
        self._save_gui_preferences()
        self._apply_auto_refresh_info_label()
        if self.auto_refresh_var.get():
            self._schedule_next_auto_refresh()
        elif self.auto_refresh_job:
            self.root.after_cancel(self.auto_refresh_job)
            self.auto_refresh_job = None
            self.next_auto_refresh_at = None
        if not self.auto_refresh_var.get() and self.auto_refresh_countdown_job:
            self.root.after_cancel(self.auto_refresh_countdown_job)
            self.auto_refresh_countdown_job = None

    def toggle_scheduler(self) -> None:
        if self.current_scheduler_state == "RUNNING":
            self._execute_action(self.stop_script, "Stop")
        else:
            self._execute_action(self.start_script, "Start")

    def restart_scheduler(self) -> None:
        self._execute_action(self.restart_script, "Restart")

    def refresh_status(self, force_data: bool = False) -> None:
        """Scheduler status every time; ticket data when forced or stale.

        The two run separately so the buttons come back as soon as the status
        is known, while the slower MESDP pull keeps loading in the background."""
        self._set_buttons_state(tk.DISABLED)
        self._show_scheduler_state("CHECKING")

        def status_worker() -> None:
            try:
                mon_code, mon_out = self._run_ps(self.monitor_script)
                state = self._parse_scheduler_state(mon_out)
            except Exception as exc:
                mon_code, mon_out, state = 1, f"Refresh failed: {exc}", ("UNKNOWN", "")
            self.root.after(0, lambda: self._on_status_done(mon_code, mon_out, state))
        threading.Thread(target=status_worker, daemon=True).start()

        stale = time.time() - (self.last_data_at or 0) >= self.DATA_MIN_INTERVAL_SEC
        if not self._data_loading and (force_data or stale):
            self._data_loading = True
            if not self.latest_tickets:
                self.table_empty_var.set("Loading ticket data from MESDP… (about a minute)")
            self.data_updated_var.set("Loading…")

            def data_worker() -> None:
                try:
                    data = self._fetch_dashboard_data()
                except Exception as exc:
                    data = {"error": str(exc)}
                self.root.after(0, lambda: self._apply_dashboard_data(data))
            threading.Thread(target=data_worker, daemon=True).start()

    def _on_status_done(self, mon_code: int, mon_out: str, state: tuple[str, str]) -> None:
        self._apply_status_payload(mon_code, mon_out, state)
        self._set_buttons_state(tk.NORMAL)
        self._schedule_next_auto_refresh()

def main() -> None:
    if "--schedule" in sys.argv:
        original_print = builtins.print

        def safe_print(*args, **kwargs):
            try:
                original_print(*args, **kwargs)
            except UnicodeEncodeError:
                out_file = kwargs.get("file", sys.stdout)
                encoding = getattr(out_file, "encoding", None) or "utf-8"
                safe_args = [str(a).encode(encoding, errors="replace").decode(encoding, errors="replace") for a in args]
                original_print(*safe_args, **kwargs)

        builtins.print = safe_print

        # Scheduler runs headless with redirected logs on Windows.
        # Force UTF-8 to avoid cp1252/charmap crashes when printing emoji/text.
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

        import worklog_reminder as wr
        if not wr.validate_config():
            print("\n❌ Config validation failed. Exiting.\n")
            sys.exit(1)
        wr.schedule_reminders()
        return

    root = ctk.CTk()
    SchedulerGui(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if "--schedule" in sys.argv:
            print(f"Failed to start scheduler mode: {exc}")
        else:
            messagebox.showerror("MESDP Scheduler Control", f"Failed to start: {exc}")
