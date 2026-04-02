import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import customtkinter as ctk  # pyright: ignore[reportMissingImports]


# ── Design Tokens ─────────────────────────────────────────────────────────────
BG          = "#080d14"
SURFACE     = "#0d1521"
SURFACE_ALT = "#111d2e"
BORDER      = "#1a2840"
BORDER_LT   = "#223354"

TEXT        = "#c8d8f0"
TEXT_MUTED  = "#5a7a9e"
TEXT_DIM    = "#2e4a68"

ACCENT      = "#2f80ed"
ACCENT_HVR  = "#1a6fd4"
GOOD        = "#16a34a"
GOOD_DIM    = "#052e16"
WARN        = "#d97706"
WARN_DIM    = "#3d2000"
BAD         = "#dc2626"
BAD_DIM     = "#3b0c0c"

FONT_MONO   = "Segoe UI"
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
        self.root.geometry("1240x820")
        self.root.minsize(1100, 700)

        self.project_root        = self._resolve_project_root()
        self.start_script        = os.path.join(self.project_root, "start_scheduler.ps1")
        self.stop_script         = os.path.join(self.project_root, "stop_scheduler.ps1")
        self.restart_script      = os.path.join(self.project_root, "restart_scheduler.ps1")
        self.monitor_script      = os.path.join(self.project_root, "monitor_scheduler.ps1")
        self.python_exe          = os.path.join(self.project_root, ".venv", "Scripts", "python.exe")
        self.shift_settings_file = os.path.join(self.project_root, "shift_settings.json")
        self.gui_settings_file   = os.path.join(self.project_root, "scheduler_gui_settings.json")

        self.auto_refresh_ms             = 30000
        self.auto_refresh_job            = None
        self.auto_refresh_countdown_job  = None
        self.next_auto_refresh_at: float | None = None
        self.current_scheduler_state     = "UNKNOWN"
        self.settings_window             = None
        self.latest_tickets: list[dict]  = []
        self._table_resize_job: str | None        = None
        self._title_tooltip: ctk.CTkToplevel | None        = None
        self._title_tooltip_label: ctk.CTkLabel | None     = None
        self._title_hover_item: str | None        = None
        self._full_title_by_item: dict[str, str]  = {}
        self._user_resized_columns = False
        self.output_window: ctk.CTkToplevel | None = None
        self.output: ctk.CTkTextbox | None = None
        self.latest_monitor_output = ""

        self.status_filter_var    = tk.StringVar(value="All")
        self.severity_filter_var  = tk.StringVar(value="All")
        self.table_count_var      = tk.StringVar(value="Showing 0 of 0")

        self.shift_hour_vars: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}
        self.shift_reminder_vars: dict[str, tk.StringVar] = {}
        self.shift_preview_var         = tk.StringVar(value="Reminder preview: —")
        self.auto_refresh_var          = tk.BooleanVar(value=True)
        self.refresh_interval_var      = tk.StringVar(value="30")
        self.refresh_interval_unit_var = tk.StringVar(value="Seconds")
        self.refresh_profile_var       = tk.StringVar(value="Balanced")

        self._load_gui_preferences()
        self._configure_styles()
        self._build_ui()
        self.root.bind_all("<Control-comma>", lambda _e: self.open_shift_settings_window())
        self.refresh_status()
        self._start_process_watchdog()
        self._update_shift_badge()

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

    # ── Process watchdog ─────────────────────────────────────────────────────

    def _get_current_shift(self) -> str:
        try:
            with open(self.shift_settings_file, encoding="utf-8") as f:
                data = json.load(f)
            hours = data.get("shift_hours", {})
            now = datetime.now()
            now_min = now.hour * 60 + now.minute
            for shift in ("morning", "evening", "night"):
                raw = hours.get(shift, {})
                s_h, s_m = map(int, raw.get("start_time", "00:00").split(":"))
                e_h, e_m = map(int, raw.get("end_time",   "00:00").split(":"))
                s_min = s_h * 60 + s_m
                e_min = e_h * 60 + e_m
                if s_min > e_min:  # crosses midnight
                    if now_min >= s_min or now_min < e_min:
                        return shift
                else:
                    if s_min <= now_min < e_min:
                        return shift
            return "morning"
        except Exception:
            return "unknown"

    def _update_shift_badge(self) -> None:
        shift = self._get_current_shift()
        label = shift.upper()
        self.shift_badge.configure(text=label, fg_color="#0d2240", text_color=ACCENT)
        self.root.after(60_000, self._update_shift_badge)

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
        cmd = ("$r=Get-CimInstance Win32_Process"
               "|Where-Object{$_.CommandLine -and "
               "$_.CommandLine -like '*worklog_reminder.py*--schedule*'};"
               "if($r){'RUNNING'}else{'NOT RUNNING'}")
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
        cfg = {
            "RUNNING":     (GOOD_DIM, GOOD, "RUNNING"),
            "NOT RUNNING": (BAD_DIM,  BAD,  "STOPPED"),
        }.get(new_state, (SURFACE_ALT, TEXT_MUTED, "UNKNOWN"))
        self.scheduler_badge.configure(fg_color=cfg[0], text_color=cfg[1], text=cfg[2])
        self.scheduler_detail.set(
            "Scheduler is active and waiting for reminder times."
            if new_state == "RUNNING" else "Scheduler process is stopped.")
        self._set_start_stop_button_for_state(new_state)

    def _make_card(self, parent) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=6,
                             border_width=1, border_color=BORDER)

    def _make_badge(self, parent, text: str, fg: str, txt: str = TEXT) -> ctk.CTkLabel:
        return ctk.CTkLabel(parent, text=text, fg_color=fg, text_color=txt,
                             font=(FONT_MONO, 11, "bold"), corner_radius=4,
                             padx=10, pady=4)

    # ── Styles ────────────────────────────────────────────────────────────────

    def _configure_styles(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root.configure(fg_color=BG)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Tickets.Treeview",
            background=SURFACE, foreground=TEXT,
            fieldbackground=SURFACE, bordercolor=SURFACE,
            rowheight=40, font=(FONT_UI, 12))
        style.configure("Tickets.Treeview.Heading",
            background=SURFACE_ALT, foreground=TEXT_MUTED,
            bordercolor=BORDER, font=(FONT_UI, 11, "bold"),
            relief="flat", padding=(8, 6))
        style.map("Tickets.Treeview",
            background=[("selected", "#162540")],
            foreground=[("selected", TEXT)])

    # ── UI Build ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self.root, fg_color=BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Header
        hcard = self._make_card(outer)
        hcard.pack(fill=tk.X, pady=(0, 14))
        hinner = ctk.CTkFrame(hcard, fg_color="transparent")
        hinner.pack(padx=20, pady=14)
        ctk.CTkLabel(hinner, text="🎫 CLL MESDP TICKETING WORKLOG",
             text_color=TEXT, font=(FONT_EMOJI, 22, "bold")).pack()
        ctk.CTkLabel(hinner,
                 text="🧭 Scheduler monitor  ·  🔔 worklog reminder  ·  📸 shift snapshot",
                 text_color=TEXT_MUTED, font=(FONT_EMOJI, 14)).pack(pady=(4, 0))

        # Toolbar
        toolbar = ctk.CTkFrame(outer, fg_color="transparent")
        toolbar.pack(fill=tk.X, pady=(0, 14))

        btn_cfg = dict(width=124, height=38, corner_radius=4, font=(FONT_UI, 11, "bold"))
        ghost   = dict(fg_color=SURFACE_ALT, hover_color=BORDER_LT, text_color=TEXT,
                       border_width=1, border_color=BORDER)

        self.start_stop_btn = ctk.CTkButton(
            toolbar, text="🟢 START",
            fg_color=GOOD_DIM, hover_color="#073a1a", text_color=GOOD,
            border_width=1, border_color=GOOD,
            command=self.toggle_scheduler, **btn_cfg)
        self.restart_btn = ctk.CTkButton(
            toolbar, text="🔁 RESTART", command=self.restart_scheduler, **btn_cfg, **ghost)
        self.refresh_btn = ctk.CTkButton(
            toolbar, text="🔄 REFRESH", command=self.refresh_status, **btn_cfg, **ghost)
        self.settings_btn = ctk.CTkButton(
            toolbar, text="🛠 SETTINGS", text_color=TEXT_MUTED,
            fg_color=SURFACE_ALT, hover_color=BORDER_LT,
            border_width=1, border_color=BORDER,
            command=self.open_shift_settings_window, **btn_cfg)
        self.terminal_btn = ctk.CTkButton(
            toolbar, text="💻 TERMINAL", command=self.open_output_window,
            **btn_cfg, **ghost)

        for i, btn in enumerate([self.start_stop_btn, self.restart_btn,
                                   self.refresh_btn, self.settings_btn, self.terminal_btn]):
            btn.grid(row=0, column=i, padx=(0, 8))

        toolbar.columnconfigure(5, weight=1)
        meta = ctk.CTkFrame(toolbar, fg_color="transparent")
        meta.grid(row=0, column=6, sticky="e")

        self.auto_refresh_info_var = tk.StringVar(value="🔄 Auto Refresh: 30 secs")
        ctk.CTkLabel(meta, textvariable=self.auto_refresh_info_var,
             text_color=TEXT_DIM, font=(FONT_EMOJI, 12)).pack(anchor="e")

        self.auto_refresh_count_var = tk.StringVar(value="⏳ Count: 00:30")
        ctk.CTkLabel(meta, textvariable=self.auto_refresh_count_var,
             text_color=TEXT_DIM, font=(FONT_EMOJI, 12)).pack(anchor="e", pady=(2, 0))

        # Status cards
        cards = ctk.CTkFrame(outer, fg_color="transparent")
        cards.pack(fill=tk.X, pady=(0, 14))
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)

        sc = self._make_card(cards)
        sc.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(sc, text="🧭 SCHEDULER", text_color=TEXT_MUTED,
                 font=(FONT_EMOJI, 12, "bold")).pack(anchor="w", padx=16, pady=(14, 6))

        status_row = ctk.CTkFrame(sc, fg_color="transparent")
        status_row.pack(anchor="w", padx=16, pady=(0, 4))
        ctk.CTkLabel(status_row, text="STATUS:", text_color=TEXT_MUTED,
                 font=(FONT_UI, 12, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        self.scheduler_badge = self._make_badge(status_row, "CHECKING…", SURFACE_ALT, TEXT_MUTED)
        self.scheduler_badge.pack(side=tk.LEFT)

        shift_row = ctk.CTkFrame(sc, fg_color="transparent")
        shift_row.pack(anchor="w", padx=16, pady=(0, 8))
        ctk.CTkLabel(shift_row, text="CURRENT SHIFT:", text_color=TEXT_MUTED,
                 font=(FONT_UI, 12, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        self.shift_badge = self._make_badge(shift_row, "DETECTING…", SURFACE_ALT, TEXT_MUTED)
        self.shift_badge.pack(side=tk.LEFT)

        self.scheduler_detail = tk.StringVar(value="Reading scheduler monitor…")
        ctk.CTkLabel(sc, textvariable=self.scheduler_detail, text_color=TEXT_MUTED,
                     font=(FONT_UI, 13), justify=tk.LEFT,
                     wraplength=520).pack(anchor="w", padx=16, pady=(0, 14))

        snap = self._make_card(cards)
        snap.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(snap, text="📸 PREVIOUS SHIFT SNAPSHOT", text_color=TEXT_MUTED,
                 font=(FONT_EMOJI, 12, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        self.total_ticket_badge = self._make_badge(snap, "TOTAL TICKET: --", SURFACE_ALT, TEXT_MUTED)
        self.total_ticket_badge.pack(anchor="w", padx=16, pady=(0, 4))
        self.updated_today_badge = self._make_badge(snap, "UPDATED TODAY: --", SURFACE_ALT, TEXT_MUTED)
        self.updated_today_badge.pack(anchor="w", padx=16, pady=(0, 4))
        self.prev_shift_badge = self._make_badge(snap, "UPDATE PENDING: --", SURFACE_ALT, TEXT_MUTED)
        self.prev_shift_badge.pack(anchor="w", padx=16, pady=(0, 4))
        self.prev_shift_badge.pack_configure(pady=(0, 8))
        self.prev_shift_detail = tk.StringVar(value="Pulling ticket data from API…")
        ctk.CTkLabel(snap, textvariable=self.prev_shift_detail, text_color=TEXT_MUTED,
                     font=(FONT_UI, 13), justify=tk.LEFT,
                     wraplength=520).pack(anchor="w", padx=16, pady=(0, 14))

        # Ticket table
        tcard = self._make_card(outer)
        tcard.pack(fill=tk.BOTH, expand=True)

        thead = ctk.CTkFrame(tcard, fg_color="transparent")
        thead.pack(fill=tk.X, padx=16, pady=(14, 10))
        ctk.CTkLabel(thead, text="🎫 PENDING TICKETS", text_color=TEXT_MUTED,
                 font=(FONT_EMOJI, 12, "bold")).pack(side=tk.LEFT)
        ctk.CTkLabel(thead, textvariable=self.table_count_var,
                     text_color=TEXT_DIM, font=(FONT_MONO, 12)).pack(side=tk.RIGHT)

        fbar = ctk.CTkFrame(tcard, fg_color="transparent")
        fbar.pack(fill=tk.X, padx=16, pady=(0, 10))
        combo_cfg = dict(width=160, height=34, font=(FONT_UI, 11))

        ctk.CTkLabel(fbar, text="Status", text_color=TEXT_MUTED,
                     font=(FONT_UI, 11)).pack(side=tk.LEFT)
        self.status_filter_combo = ctk.CTkComboBox(
            fbar, values=["All"], variable=self.status_filter_var,
            state="readonly", command=lambda _v: self._on_filter_changed(), **combo_cfg)
        self.status_filter_combo.pack(side=tk.LEFT, padx=(6, 16))
        self.status_filter_combo.set("All")

        ctk.CTkLabel(fbar, text="Severity", text_color=TEXT_MUTED,
                     font=(FONT_UI, 11)).pack(side=tk.LEFT)
        self.severity_filter_combo = ctk.CTkComboBox(
            fbar, values=["All"], variable=self.severity_filter_var,
            state="readonly", command=lambda _v: self._on_filter_changed(), **combo_cfg)
        self.severity_filter_combo.pack(side=tk.LEFT, padx=(6, 16))
        self.severity_filter_combo.set("All")

        ctk.CTkButton(fbar, text="Clear", width=86, height=34,
                      fg_color=SURFACE_ALT, hover_color=BORDER_LT,
                      text_color=TEXT_MUTED, border_width=1, border_color=BORDER,
                  font=(FONT_UI, 10), command=self._clear_filters).pack(side=tk.LEFT)

        self.table_wrap = ctk.CTkFrame(tcard, fg_color="transparent")
        self.table_wrap.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 14))

        cols    = ("ticket_id", "title", "assigned_l1", "severity", "status", "last_worklog")
        headers = {"ticket_id": "Ticket ID", "title": "Title", "assigned_l1": "Assigned L1",
                   "severity": "Severity", "status": "Status", "last_worklog": "Last Worklog"}
        widths  = {"ticket_id": 90, "title": 360, "assigned_l1": 130,
                   "severity": 100, "status": 130, "last_worklog": 140}
        self.ticket_column_widths = widths

        self.ticket_table = ttk.Treeview(
            self.table_wrap, columns=cols, show="headings", style="Tickets.Treeview")
        for col in cols:
            self.ticket_table.heading(col, text=headers[col])
            anchor = tk.CENTER
            if col == "title":
                self.ticket_table.column(col, width=widths[col], minwidth=self.TITLE_MIN_WIDTH,
                                         stretch=True, anchor=anchor)
            else:
                self.ticket_table.column(col, width=widths[col], minwidth=max(70, int(widths[col] * 0.6)),
                                         stretch=False, anchor=anchor)

        sy = ttk.Scrollbar(self.table_wrap, orient=tk.VERTICAL, command=self.ticket_table.yview)
        sx = ttk.Scrollbar(self.table_wrap, orient=tk.HORIZONTAL, command=self.ticket_table.xview)
        self.ticket_table.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.ticket_table.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        sx.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.table_wrap.grid_rowconfigure(0, weight=1)
        self.table_wrap.grid_columnconfigure(0, weight=1)
        self.ticket_table.bind("<Configure>", self._on_ticket_table_configure)
        self.ticket_table.bind("<ButtonRelease-1>", self._on_ticket_table_mouse_release)
        self.ticket_table.bind("<Motion>",    self._on_ticket_table_hover)
        self.ticket_table.bind("<Leave>",     self._hide_title_tooltip)

    def open_output_window(self) -> None:
        if self.output_window and self.output_window.winfo_exists():
            self.output_window.lift()
            self.output_window.focus_force()
            return

        win = ctk.CTkToplevel(self.root)
        win.title("Monitor Output")
        win.geometry("920x360")
        win.minsize(760, 260)
        win.configure(fg_color=SURFACE)
        win.transient(self.root)
        win.lift()
        win.focus_force()
        self.output_window = win

        c = ctk.CTkFrame(win, fg_color=SURFACE)
        c.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        ctk.CTkLabel(c, text="MONITOR OUTPUT", text_color=TEXT_MUTED,
                     font=(FONT_MONO, 10, "bold")).pack(anchor="w", pady=(0, 8))
        self.output = ctk.CTkTextbox(c, font=(FONT_MONO, 11),
                                     fg_color=BG, text_color="#4a7098",
                                     border_width=1, border_color=BORDER, corner_radius=4)
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
        self._resize_title_column()
        if self.latest_tickets:
            self._render_filtered_ticket_rows()

    def _on_ticket_table_mouse_release(self, event) -> None:
        try:
            if self.ticket_table.identify_region(event.x, event.y) == "separator":
                self._user_resized_columns = True
        except Exception:
            pass

    def _resize_title_column(self) -> None:
        if self._user_resized_columns:
            return
        avail = self.table_wrap.winfo_width()
        if avail <= 1:
            return
        fixed  = sum(self.ticket_column_widths[c] for c in
                     ("ticket_id", "assigned_l1", "severity", "status", "last_worklog"))
        target = max(self.TITLE_MIN_WIDTH, avail - fixed - 32)
        self.ticket_table.column("title", width=target,
                                  minwidth=self.TITLE_MIN_WIDTH, stretch=True)

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
        self.table_count_var.set(f"Showing {len(filtered)} of {len(self.latest_tickets)}")

    def _render_ticket_rows(self, tickets: list[dict]) -> None:
        self._hide_title_tooltip()
        self._full_title_by_item.clear()
        for row in self.ticket_table.get_children():
            self.ticket_table.delete(row)
        for item in tickets:
            raw   = str(item.get("title", "N/A"))
            short = self._truncate_title_for_current_width(raw)
            rid   = self.ticket_table.insert("", tk.END, values=(
                item.get("ticket_id",    "N/A"),
                short,
                item.get("assigned_l1",  "N/A"),
                item.get("severity",     "N/A"),
                item.get("status",       "N/A"),
                item.get("last_worklog", "N/A"),
            ))
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
        p = subprocess.run(cmd, **kw)
        return p.returncode, ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()

    def _run_python_inline(self, code_text: str) -> tuple[int, str]:
        if not os.path.exists(self.python_exe):
            return 1, f"Python not found: {self.python_exe}"
        cmd = [self.python_exe, "-X", "utf8", "-c", code_text]
        kw: dict = {"cwd": self.project_root, "capture_output": True,
                    "text": True, "encoding": "utf-8", "errors": "replace"}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kw["startupinfo"] = si
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.run(cmd, **kw)
        return p.returncode, ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()

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
                self.shift_reminder_vars[sn].set(str(offsets.get(sn, 30)))
        self._refresh_shift_preview(parsed.get("reminders", {}))

    def open_shift_settings_window(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift(); self.settings_window.focus_force(); return

        win = ctk.CTkToplevel(self.root)
        win.title("Shift Settings  (Ctrl+,)")
        win.geometry("920x620")
        win.resizable(False, False)
        win.configure(fg_color=SURFACE)
        win.transient(self.root)
        win.lift()
        win.focus_force()
        self.settings_window = win

        c = ctk.CTkFrame(win, fg_color=SURFACE)
        c.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        c.grid_columnconfigure(0, minsize=190)
        c.grid_columnconfigure(1, minsize=125)
        c.grid_columnconfigure(2, minsize=125)
        c.grid_columnconfigure(3, minsize=180)

        ctk.CTkLabel(c, text="SHIFT SETTINGS", text_color=TEXT_MUTED,
                     font=(FONT_MONO, 14, "bold")).grid(
            row=0, column=0, columnspan=7, sticky="w", pady=(0, 12))

        lbl_cfg = dict(text_color=TEXT_MUTED, font=(FONT_UI, 14, "bold"))
        for col, text in enumerate(["Shift", "Start (HH:MM)", "End (HH:MM)",
                                    "Reminder (min before end)"]):
            ctk.CTkLabel(c, text=text, **lbl_cfg).grid(
                row=1, column=col, padx=(0, 10), pady=(0, 6), sticky="w")

        self.shift_hour_vars     = {}
        self.shift_reminder_vars = {}
        entry_cfg = dict(width=104, height=34, fg_color=BG, text_color=TEXT,
                 border_color=BORDER, border_width=1, font=(FONT_MONO, 14))

        for ri, sn in enumerate(("morning", "evening", "night"), start=2):
            ctk.CTkLabel(c, text=sn.capitalize(), text_color=TEXT,
                         font=(FONT_UI, 14, "bold")).grid(
                row=ri, column=0, padx=(0, 10), pady=(0, 8), sticky="w")
            sv, ev, rv = tk.StringVar(), tk.StringVar(), tk.StringVar(value="30")
            for ci, var in enumerate([sv, ev, rv], start=1):
                e = ctk.CTkEntry(c, textvariable=var, **entry_cfg)
                e.grid(row=ri, column=ci, padx=(0, 10), pady=(0, 8), sticky="w")
                e.bind("<KeyRelease>", lambda _e: self._refresh_shift_preview())
            self.shift_hour_vars[sn]     = (sv, ev)
            self.shift_reminder_vars[sn] = rv

        btn_row = 5
        ghost_btn = dict(border_width=1, border_color=BORDER, font=(FONT_UI, 14, "bold"))

        btn_frame = ctk.CTkFrame(c, fg_color="transparent")
        btn_frame.grid(row=btn_row, column=0, columnspan=7, pady=(4, 0), sticky="w")
        for txt, fg, hvr, cmd in [
            ("SAVE",          ACCENT,      ACCENT_HVR, self.save_shift_hours),
            ("RESET DEFAULT", SURFACE_ALT, BORDER_LT,  self.reset_settings_to_default),
            ("CLOSE",         SURFACE_ALT, BORDER_LT,  win.destroy),
        ]:
            ctk.CTkButton(btn_frame, text=txt, width=160, height=38,
                          fg_color=fg, hover_color=hvr, text_color=TEXT,
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
                      fg_color=ACCENT, hover_color=ACCENT_HVR, text_color=TEXT,
                  font=(FONT_UI, 14, "bold"),
                      command=self.save_auto_refresh_only).grid(
            row=btn_row + 7, column=0, padx=(0, 16), pady=(8, 0), sticky="w")

        self._load_shift_hours_into_form()
        self._apply_profile_to_interval(set_custom_if_manual=False)
        self._apply_auto_refresh_info_label()

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

    def _refresh_shift_preview(self, reminders: dict | None = None) -> None:
        if reminders is None:
            reminders = {}
            for sn, (_, ev) in self.shift_hour_vars.items():
                try:
                    hh, mm  = ev.get().strip().split(":")
                    end_min = int(hh) * 60 + int(mm)
                    offset  = int(self.shift_reminder_vars[sn].get().strip())
                    rem     = (end_min - offset) % (24 * 60)
                    reminders[sn] = f"{rem // 60:02d}:{rem % 60:02d}"
                except (ValueError, AttributeError, KeyError):
                    reminders[sn] = "--:--"
        self.shift_preview_var.set(
            f"Preview  ·  M {reminders.get('morning','--:--')}  "
            f"E {reminders.get('evening','--:--')}  "
            f"N {reminders.get('night','--:--')}")

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
                rm     = int(self.shift_reminder_vars[sn].get().strip())
                sh, sm, eh, em = int(sh), int(sm), int(eh), int(em)
            except ValueError:
                messagebox.showerror("Invalid Input",
                    f"{sn.capitalize()}: use HH:MM format and integer reminder."); return
            if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
                messagebox.showerror("Invalid Time",
                    f"{sn.capitalize()}: time out of valid range."); return
            if not (0 <= rm <= 720):
                messagebox.showerror("Invalid Reminder",
                    f"{sn.capitalize()}: reminder must be 0–720 min."); return
            new_hours[sn] = {"start_time": f"{sh:02d}:{sm:02d}",
                              "end_time":   f"{eh:02d}:{em:02d}",
                              "reminder_minutes": rm}
        try:
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
        for sn, (s, e, r) in {"morning": ("07:00","15:00","30"),
                               "evening": ("15:00","23:00","30"),
                               "night":   ("23:00","07:00","30")}.items():
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

    def _fetch_previous_shift_snapshot(self) -> dict:
        code = (
            "import json, sys\n"
            f"sys.path.insert(0, {self.project_root!r})\n"
            "import worklog_reminder as wr\n"
            "order=['morning','evening','night']\n"
            "current=wr.get_auto_shift()\n"
            "previous=order[(order.index(current)-1)%3]\n"
            "summary=wr.get_inprogress_summary() or {}\n"
            "tickets=summary.get('tickets', [])\n"
            "pending=summary.get('pending_count', len(tickets))\n"
            "updated=summary.get('updated_today_count', 0)\n"
            "total=summary.get('total_in_progress', pending + updated)\n"
            "print(json.dumps({'current_shift':current,'previous_shift':previous,"
            "'pending_count':pending,'updated_today_count':updated,'total_in_progress':total,'tickets':tickets[:25]}))\n"
        )
        rc, out = self._run_python_inline(code)
        if rc != 0:
            return {"error": out or "Failed to pull snapshot."}
        for line in reversed(out.splitlines()):
            line = line.strip()
            if not line: continue
            try: return json.loads(line)
            except json.JSONDecodeError: continue
        return {"error": out or "No JSON payload returned."}

    def _update_ticket_table(self, tickets: list[dict]) -> None:
        self.latest_tickets = tickets
        self._refresh_filter_options(tickets)
        self._resize_title_column()
        self._render_filtered_ticket_rows()

    def _apply_refresh_payload(self, payload: dict) -> None:
        mon_code, mon_out          = payload["monitor_code"], payload["monitor_output"]
        scheduler_state, sched_msg = payload["scheduler_state"]
        snapshot                   = payload["snapshot"]

        self._set_output(mon_out or "No monitor output.")
        if mon_code != 0:
            self.current_scheduler_state = "UNKNOWN"
            self.scheduler_badge.configure(text="ERROR", fg_color=BAD_DIM, text_color=BAD)
        else:
            self.current_scheduler_state = scheduler_state
            cfg = {
                "RUNNING":     (GOOD_DIM, GOOD, "RUNNING"),
                "NOT RUNNING": (BAD_DIM,  BAD,  "STOPPED"),
            }.get(scheduler_state, (SURFACE_ALT, TEXT_MUTED, "UNKNOWN"))
            self.scheduler_badge.configure(fg_color=cfg[0], text_color=cfg[1], text=cfg[2])
            self.scheduler_detail.set(sched_msg)

        self._set_start_stop_button_for_state(self.current_scheduler_state)

        if "error" in snapshot:
            self.total_ticket_badge.configure(text="TOTAL TICKET: --", fg_color=BAD_DIM, text_color=BAD)
            self.prev_shift_badge.configure(text="API ERROR", fg_color=BAD_DIM, text_color=BAD)
            self.updated_today_badge.configure(text="UPDATED TODAY: --", fg_color=BAD_DIM, text_color=BAD)
            self.prev_shift_detail.set("Unable to load ticket snapshot.")
            self._update_ticket_table([]); return

        prev    = str(snapshot.get("previous_shift", "N/A")).upper()
        count   = int(snapshot.get("pending_count", 0))
        updated = int(snapshot.get("updated_today_count", 0))
        total   = int(snapshot.get("total_in_progress", count + updated))
        tickets = snapshot.get("tickets", [])
        clr     = GOOD_DIM if count == 0 else WARN_DIM
        txt_clr = GOOD     if count == 0 else WARN
        self.total_ticket_badge.configure(text=f"TOTAL TICKET: {total}", fg_color=ACCENT_HVR, text_color=TEXT)
        self.prev_shift_badge.configure(text=f"UPDATE PENDING: {count}", fg_color=clr, text_color=txt_clr)
        self.updated_today_badge.configure(text=f"UPDATED TODAY: {updated}", fg_color=GOOD_DIM, text_color=GOOD)
        self.prev_shift_detail.set(
            f"Previous shift: {prev}  ·  Pending carry-over tickets from API snapshot.")
        self._update_ticket_table(tickets)

    def _set_output(self, text: str) -> None:
        self.latest_monitor_output = text
        if self.output and self.output.winfo_exists():
            self.output.configure(state=tk.NORMAL)
            self.output.delete("1.0", tk.END)
            self.output.insert("1.0", text)
            self.output.configure(state=tk.DISABLED)

    def _execute_action(self, script_path: str, action_name: str) -> None:
        def worker() -> None:
            self._set_buttons_state(tk.DISABLED)
            code, out = self._run_ps(script_path)
            self._set_output(out or f"No output for {action_name}.")
            self._set_buttons_state(tk.NORMAL)
            self.refresh_status()
        threading.Thread(target=worker, daemon=True).start()

    def _set_buttons_state(self, state: str) -> None:
        for btn in [self.start_stop_btn, self.restart_btn,
                    self.refresh_btn,    self.settings_btn]:
            btn.configure(state=state)

    def _apply_auto_refresh_info_label(self) -> None:
        self._update_auto_refresh_countdown_label()

    def _update_auto_refresh_countdown_label(self) -> None:
        if self.auto_refresh_countdown_job:
            self.root.after_cancel(self.auto_refresh_countdown_job)
            self.auto_refresh_countdown_job = None

        if not self.auto_refresh_var.get():
            self.auto_refresh_info_var.set("🔄 Auto Refresh: Off")
            self.auto_refresh_count_var.set("⏳ Count: --:--")
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

        self.auto_refresh_info_var.set(f"🔄 Auto Refresh: {unit_text}")
        self.auto_refresh_count_var.set(f"⏳ Count: {countdown}")
        self.auto_refresh_countdown_job = self.root.after(1000, self._update_auto_refresh_countdown_label)

    def _set_start_stop_button_for_state(self, state: str) -> None:
        if state == "RUNNING":
            self.start_stop_btn.configure(
                text="🛑 STOP", fg_color=BAD_DIM, hover_color="#5a1010",
                text_color=BAD, border_color=BAD)
        else:
            self.start_stop_btn.configure(
                text="🟢 START", fg_color=GOOD_DIM, hover_color="#073a1a",
                text_color=GOOD, border_color=GOOD)

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

    def refresh_status(self) -> None:
        self._set_buttons_state(tk.DISABLED)
        self.scheduler_badge.configure(text="…", fg_color=SURFACE_ALT, text_color=TEXT_MUTED)
        self.prev_shift_badge.configure(text="…", fg_color=SURFACE_ALT, text_color=TEXT_MUTED)

        def worker() -> None:
            mon_code, mon_out = self._run_ps(self.monitor_script)
            scheduler_state   = self._parse_scheduler_state(mon_out)
            snapshot          = self._fetch_previous_shift_snapshot()
            payload = {"monitor_code": mon_code, "monitor_output": mon_out,
                       "scheduler_state": scheduler_state, "snapshot": snapshot}
            self.root.after(0, lambda: self._on_refresh_done(payload))
        threading.Thread(target=worker, daemon=True).start()

    def _on_refresh_done(self, payload: dict) -> None:
        self._apply_refresh_payload(payload)
        self._set_buttons_state(tk.NORMAL)
        self._schedule_next_auto_refresh()


def main() -> None:
    root = ctk.CTk()
    SchedulerGui(root)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        messagebox.showerror("MESDP Scheduler Control", f"Failed to start: {exc}")
