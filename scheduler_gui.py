import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from datetime import datetime


class SchedulerGui:
    REFRESH_PROFILES = {
        "Realtime": 10,
        "Balanced": 30,
        "Low API Load": 120,
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MESDP Scheduler Control")
        self.root.geometry("1080x720")
        self.root.minsize(960, 620)

        self.project_root = self._resolve_project_root()
        self.start_script = os.path.join(self.project_root, "start_scheduler.ps1")
        self.stop_script = os.path.join(self.project_root, "stop_scheduler.ps1")
        self.restart_script = os.path.join(self.project_root, "restart_scheduler.ps1")
        self.monitor_script = os.path.join(self.project_root, "monitor_scheduler.ps1")
        self.python_exe = os.path.join(self.project_root, ".venv", "Scripts", "python.exe")
        self.shift_settings_file = os.path.join(self.project_root, "shift_settings.json")
        self.gui_settings_file = os.path.join(self.project_root, "scheduler_gui_settings.json")

        self.bg = "#0b1220"
        self.card = "#111a2d"
        self.card_alt = "#0f1a30"
        self.text_main = "#e5edf8"
        self.text_muted = "#9cb0cd"
        self.accent = "#38bdf8"
        self.good = "#22c55e"
        self.warn = "#f59e0b"
        self.bad = "#ef4444"
        self.auto_refresh_ms = 30000
        self.auto_refresh_job = None
        self.current_scheduler_state = "UNKNOWN"
        self.settings_window = None
        self.shift_hour_vars: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}
        self.shift_reminder_vars: dict[str, tk.StringVar] = {}
        self.shift_preview_var = tk.StringVar(value="Reminder preview: -")
        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.refresh_interval_var = tk.StringVar(value="30")
        self.refresh_interval_unit_var = tk.StringVar(value="Seconds")
        self.refresh_profile_var = tk.StringVar(value="Balanced")

        self._load_gui_preferences()
        self._configure_styles()
        self._build_ui()
        self.root.bind_all("<Control-comma>", lambda _e: self.open_shift_settings_window())
        self.refresh_status()

    def _load_gui_preferences(self) -> None:
        if not os.path.exists(self.gui_settings_file):
            return
        try:
            with open(self.gui_settings_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            auto_refresh = bool(payload.get("auto_refresh", True))
            interval_seconds = int(payload.get("refresh_interval_seconds", 30))
            interval_value = str(payload.get("refresh_interval_value", ""))
            interval_unit = str(payload.get("refresh_interval_unit", ""))
            refresh_profile = str(payload.get("refresh_profile", "Balanced"))
            if interval_seconds < 5:
                interval_seconds = 5
            if interval_seconds > 86400:
                interval_seconds = 86400
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
            "auto_refresh": bool(self.auto_refresh_var.get()),
            "refresh_interval_seconds": int(self.auto_refresh_ms / 1000),
            "refresh_interval_value": self.refresh_interval_var.get(),
            "refresh_interval_unit": self.refresh_interval_unit_var.get(),
            "refresh_profile": self.refresh_profile_var.get(),
        }
        try:
            with open(self.gui_settings_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError:
            pass

    def _resolve_project_root(self) -> str:
        """Find the folder that contains scheduler control scripts."""
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        file_dir = os.path.dirname(os.path.abspath(__file__))
        cwd = os.getcwd()

        candidates = [
            file_dir,
            exe_dir,
            os.path.dirname(exe_dir),
            os.path.dirname(os.path.dirname(exe_dir)),
            cwd,
        ]

        required = ("start_scheduler.ps1", "stop_scheduler.ps1", "monitor_scheduler.ps1")
        for base in candidates:
            if all(os.path.exists(os.path.join(base, name)) for name in required):
                return base

        return file_dir

    def _configure_styles(self) -> None:
        self.root.configure(bg=self.bg)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Tickets.Treeview",
            background=self.card,
            foreground=self.text_main,
            fieldbackground=self.card,
            bordercolor=self.card,
            rowheight=30,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Tickets.Treeview.Heading",
            background="#1f2a44",
            foreground="#dbe7fb",
            bordercolor="#1f2a44",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
        )
        style.map(
            "Tickets.Treeview",
            background=[("selected", "#213453")],
            foreground=[("selected", "#ffffff")],
        )

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=self.bg)
        outer.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        header = tk.Frame(outer, bg=self.card_alt, highlightthickness=1, highlightbackground="#1f2a44")
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text="MESDP Control Launcher",
            bg=self.card_alt,
            fg=self.text_main,
            font=("Segoe UI", 20, "bold"),
            pady=12,
        ).pack()
        tk.Label(
            header,
            text="Start, stop, restart scheduler and monitor previous-shift ticket status in one dashboard",
            bg=self.card_alt,
            fg=self.text_muted,
            font=("Segoe UI", 10),
            pady=0,
        ).pack(pady=(0, 12))

        controls = tk.Frame(outer, bg=self.bg)
        controls.pack(fill=tk.X, pady=(14, 10))

        self.start_stop_btn = tk.Button(
            controls,
            text="START",
            width=14,
            bg="#0f5132",
            fg="#ffffff",
            activebackground="#146c43",
            relief=tk.FLAT,
            command=self.toggle_scheduler,
        )
        self.restart_btn = tk.Button(
            controls,
            text="RESTART",
            width=14,
            bg="#7c2d12",
            fg="#ffffff",
            activebackground="#9a3412",
            relief=tk.FLAT,
            command=self.restart_scheduler,
        )
        self.refresh_btn = tk.Button(
            controls,
            text="REFRESH",
            width=14,
            bg="#0f4c81",
            fg="#ffffff",
            activebackground="#145ea8",
            relief=tk.FLAT,
            command=self.refresh_status,
        )
        self.settings_btn = tk.Button(
            controls,
            text="SETTINGS ⚙",
            width=16,
            bg="#1f2a44",
            fg="#ffffff",
            activebackground="#2c3c60",
            relief=tk.FLAT,
            command=self.open_shift_settings_window,
        )

        self.start_stop_btn.grid(row=0, column=0, padx=(0, 8))
        self.restart_btn.grid(row=0, column=1, padx=8)
        self.refresh_btn.grid(row=0, column=2, padx=8)
        self.settings_btn.grid(row=0, column=3, padx=8)

        controls.columnconfigure(4, weight=1)
        self.auto_refresh_info_var = tk.StringVar(value="Auto refresh: ON (30s)")
        tk.Label(
            controls,
            textvariable=self.auto_refresh_info_var,
            bg=self.bg,
            fg=self.text_muted,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=6, padx=(12, 0), sticky="e")

        self.last_updated_var = tk.StringVar(value="Last updated: -")
        tk.Label(
            controls,
            textvariable=self.last_updated_var,
            bg=self.bg,
            fg=self.text_muted,
            font=("Segoe UI", 9),
        ).grid(row=0, column=7, padx=(10, 0), sticky="e")

        cards = tk.Frame(outer, bg=self.bg)
        cards.pack(fill=tk.X, pady=(8, 12))
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)

        sched_card = tk.Frame(cards, bg=self.card, highlightthickness=1, highlightbackground="#1f2a44")
        sched_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(sched_card, text="SCHEDULER STATUS", bg=self.card, fg=self.text_muted, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        self.scheduler_badge = tk.Label(
            sched_card,
            text="CHECKING...",
            bg="#1f2937",
            fg="#ffffff",
            padx=10,
            pady=5,
            font=("Segoe UI", 10, "bold"),
        )
        self.scheduler_badge.pack(anchor="w", padx=12, pady=(0, 8))
        self.scheduler_detail = tk.StringVar(value="Reading scheduler monitor output...")
        tk.Label(
            sched_card,
            textvariable=self.scheduler_detail,
            bg=self.card,
            fg=self.text_main,
            justify=tk.LEFT,
            wraplength=430,
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=12, pady=(0, 12))

        shift_card = tk.Frame(cards, bg=self.card, highlightthickness=1, highlightbackground="#1f2a44")
        shift_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tk.Label(
            shift_card,
            text="PREVIOUS SHIFT TICKET SNAPSHOT",
            bg=self.card,
            fg=self.text_muted,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=12, pady=(12, 4))
        self.prev_shift_badge = tk.Label(
            shift_card,
            text="LOADING...",
            bg="#1f2937",
            fg="#ffffff",
            padx=10,
            pady=5,
            font=("Segoe UI", 10, "bold"),
        )
        self.prev_shift_badge.pack(anchor="w", padx=12, pady=(0, 8))
        self.prev_shift_detail = tk.StringVar(value="Pulling pending ticket status from API...")
        tk.Label(
            shift_card,
            textvariable=self.prev_shift_detail,
            bg=self.card,
            fg=self.text_main,
            justify=tk.LEFT,
            wraplength=430,
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=12, pady=(0, 12))

        table_card = tk.Frame(outer, bg=self.card, highlightthickness=1, highlightbackground="#1f2a44")
        table_card.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            table_card,
            text="PENDING TICKETS (PREVIOUS SHIFT REFERENCE)",
            bg=self.card,
            fg=self.text_muted,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=12, pady=(12, 6))

        columns = ("ticket_id", "title", "assigned_l1", "severity", "status", "last_worklog")
        self.ticket_table = ttk.Treeview(table_card, columns=columns, show="headings", style="Tickets.Treeview")
        headers = {
            "ticket_id": "Ticket ID",
            "title": "Title",
            "assigned_l1": "Assigned L1",
            "severity": "Severity",
            "status": "Status",
            "last_worklog": "Last Worklog",
        }
        widths = {
            "ticket_id": 90,
            "title": 360,
            "assigned_l1": 130,
            "severity": 100,
            "status": 130,
            "last_worklog": 140,
        }
        for col in columns:
            self.ticket_table.heading(col, text=headers[col])
            self.ticket_table.column(col, width=widths[col], anchor=tk.CENTER)

        table_scroll = ttk.Scrollbar(table_card, orient=tk.VERTICAL, command=self.ticket_table.yview)
        self.ticket_table.configure(yscrollcommand=table_scroll.set)

        self.ticket_table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0), pady=(0, 12))
        table_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 12), pady=(0, 12))

        log_card = tk.Frame(outer, bg=self.card, highlightthickness=1, highlightbackground="#1f2a44")
        log_card.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        tk.Label(log_card, text="MONITOR OUTPUT", bg=self.card, fg=self.text_muted, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=12, pady=(12, 6)
        )
        self.output = scrolledtext.ScrolledText(
            log_card,
            wrap=tk.WORD,
            font=("Consolas", 10),
            height=8,
            bg="#0b1324",
            fg="#cbd8ee",
            insertbackground="#cbd8ee",
            relief=tk.FLAT,
            borderwidth=0,
        )
        self.output.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        self.output.configure(state=tk.DISABLED)

    def _run_ps(self, script_path: str) -> tuple[int, str]:
        if not os.path.exists(script_path):
            return 1, f"Script not found: {script_path}"

        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_path,
        ]

        proc = subprocess.run(
            cmd,
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        combined = (proc.stdout or "")
        if proc.stderr:
            combined += "\n" + proc.stderr
        return proc.returncode, combined.strip()

    def _run_python_inline(self, code_text: str) -> tuple[int, str]:
        if not os.path.exists(self.python_exe):
            return 1, f"Python executable not found: {self.python_exe}"

        cmd = [self.python_exe, "-X", "utf8", "-c", code_text]
        proc = subprocess.run(
            cmd,
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        combined = (proc.stdout or "")
        if proc.stderr:
            combined += "\n" + proc.stderr
        return proc.returncode, combined.strip()

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

    def _interval_seconds_from_fields(self) -> int:
        raw = self.refresh_interval_var.get().strip()
        unit = self.refresh_interval_unit_var.get().strip()
        value = int(raw)
        if unit == "Hours":
            return value * 3600
        if unit == "Minutes":
            return value * 60
        return value

    def _load_shift_hours_into_form(self) -> None:
        if not self.shift_hour_vars:
            return

        code = (
            "import json, sys\n"
            f"sys.path.insert(0, {self.project_root!r})\n"
            "import worklog_reminder as wr\n"
            "out={'times': wr.SHIFT_TIMES, 'reminders': wr.SHIFT_REMINDER_TIMES, 'offsets': wr.SHIFT_REMINDER_OFFSETS}\n"
            "print(json.dumps(out))\n"
        )
        rc, out = self._run_python_inline(code)
        if rc != 0:
            return

        parsed = None
        for line in reversed(out.splitlines()):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(parsed, dict):
            return

        times = parsed.get("times", {})
        offsets = parsed.get("offsets", {})
        for shift_name, vars_pair in self.shift_hour_vars.items():
            start_var, end_var = vars_pair
            val = times.get(shift_name, ["00:00", "00:00"])
            if isinstance(val, (list, tuple)) and len(val) == 2:
                start_var.set(str(val[0]))
                end_var.set(str(val[1]))
            if shift_name in self.shift_reminder_vars:
                self.shift_reminder_vars[shift_name].set(str(offsets.get(shift_name, 30)))

        self._refresh_shift_preview(parsed.get("reminders", {}))

    def open_shift_settings_window(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("Shift Settings  (Ctrl+,)")
        win.geometry("820x420")
        win.resizable(False, False)
        win.configure(bg=self.card)
        self.settings_window = win

        container = tk.Frame(win, bg=self.card)
        container.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        tk.Label(
            container,
            text="Shift Settings",
            bg=self.card,
            fg=self.text_main,
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))

        tk.Label(container, text="Shift", bg=self.card, fg=self.text_muted, font=("Segoe UI", 9, "bold")).grid(
            row=1, column=0, padx=(6, 10), sticky="w"
        )
        tk.Label(container, text="Start (HH:MM)", bg=self.card, fg=self.text_muted, font=("Segoe UI", 9, "bold")).grid(
            row=1, column=1, padx=(0, 10), sticky="w"
        )
        tk.Label(container, text="End (HH:MM)", bg=self.card, fg=self.text_muted, font=("Segoe UI", 9, "bold")).grid(
            row=1, column=2, padx=(0, 10), sticky="w"
        )
        tk.Label(
            container,
            text="Reminder (minutes before end)",
            bg=self.card,
            fg=self.text_muted,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=1, column=3, padx=(0, 10), sticky="w")

        self.shift_hour_vars = {}
        self.shift_reminder_vars = {}
        row_idx = 2
        for shift_name in ("morning", "evening", "night"):
            tk.Label(
                container,
                text=shift_name.capitalize(),
                bg=self.card,
                fg=self.text_main,
                font=("Segoe UI", 10, "bold"),
            ).grid(row=row_idx, column=0, padx=(6, 10), pady=(0, 8), sticky="w")

            start_var = tk.StringVar()
            start_entry = tk.Entry(
                container,
                textvariable=start_var,
                width=8,
                bg="#0b1324",
                fg=self.text_main,
                insertbackground=self.text_main,
                relief=tk.FLAT,
                justify="center",
            )
            start_entry.grid(row=row_idx, column=1, padx=(0, 10), pady=(0, 8), sticky="w")
            start_entry.bind("<KeyRelease>", lambda _e: self._refresh_shift_preview())

            end_var = tk.StringVar()
            end_entry = tk.Entry(
                container,
                textvariable=end_var,
                width=8,
                bg="#0b1324",
                fg=self.text_main,
                insertbackground=self.text_main,
                relief=tk.FLAT,
                justify="center",
            )
            end_entry.grid(row=row_idx, column=2, padx=(0, 10), pady=(0, 8), sticky="w")
            end_entry.bind("<KeyRelease>", lambda _e: self._refresh_shift_preview())

            reminder_var = tk.StringVar(value="30")
            reminder_entry = tk.Entry(
                container,
                textvariable=reminder_var,
                width=8,
                bg="#0b1324",
                fg=self.text_main,
                insertbackground=self.text_main,
                relief=tk.FLAT,
                justify="center",
            )
            reminder_entry.grid(row=row_idx, column=3, padx=(0, 10), pady=(0, 8), sticky="w")
            reminder_entry.bind("<KeyRelease>", lambda _e: self._refresh_shift_preview())

            self.shift_hour_vars[shift_name] = (start_var, end_var)
            self.shift_reminder_vars[shift_name] = reminder_var
            row_idx += 1

        tk.Button(
            container,
            text="SAVE",
            bg="#0f4c81",
            fg="#ffffff",
            activebackground="#145ea8",
            relief=tk.FLAT,
            command=self.save_shift_hours,
        ).grid(row=row_idx, column=2, padx=(0, 8), pady=(4, 8), sticky="e")

        tk.Button(
            container,
            text="RESET DEFAULT",
            bg="#7c2d12",
            fg="#ffffff",
            activebackground="#9a3412",
            relief=tk.FLAT,
            command=self.reset_settings_to_default,
        ).grid(row=row_idx, column=3, padx=(0, 8), pady=(4, 8), sticky="w")

        tk.Button(
            container,
            text="CLOSE",
            bg="#374151",
            fg="#ffffff",
            activebackground="#4b5563",
            relief=tk.FLAT,
            command=win.destroy,
        ).grid(row=row_idx, column=4, padx=(0, 0), pady=(4, 8), sticky="w")

        tk.Label(
            container,
            textvariable=self.shift_preview_var,
            bg=self.card,
            fg=self.text_main,
            font=("Segoe UI", 9),
            justify=tk.LEFT,
        ).grid(row=row_idx + 1, column=0, columnspan=6, padx=(6, 0), pady=(6, 0), sticky="w")

        tk.Label(
            container,
            text="Auto Refresh Settings",
            bg=self.card,
            fg=self.text_main,
            font=("Segoe UI", 11, "bold"),
        ).grid(row=row_idx + 2, column=0, columnspan=6, sticky="w", pady=(18, 6))

        self.settings_auto_refresh_check = tk.Checkbutton(
            container,
            text="Enable Auto Refresh",
            variable=self.auto_refresh_var,
            bg=self.card,
            fg=self.text_muted,
            activebackground=self.card,
            activeforeground=self.text_main,
            selectcolor="#0b1324",
            font=("Segoe UI", 9, "bold"),
            command=self._toggle_auto_refresh,
        )
        self.settings_auto_refresh_check.grid(row=row_idx + 3, column=0, columnspan=2, padx=(6, 8), sticky="w")

        tk.Label(
            container,
            text="Profile",
            bg=self.card,
            fg=self.text_muted,
            font=("Segoe UI", 9),
        ).grid(row=row_idx + 3, column=2, padx=(8, 4), sticky="w")

        self.refresh_profile_combo = ttk.Combobox(
            container,
            textvariable=self.refresh_profile_var,
            values=["Realtime", "Balanced", "Low API Load", "Custom"],
            width=13,
            state="readonly",
        )
        self.refresh_profile_combo.grid(row=row_idx + 3, column=3, padx=(0, 8), sticky="w")
        self.refresh_profile_combo.bind("<<ComboboxSelected>>", self._on_profile_changed)

        tk.Label(
            container,
            text="Interval (seconds)",
            bg=self.card,
            fg=self.text_muted,
            font=("Segoe UI", 9),
        ).grid(row=row_idx + 3, column=4, padx=(8, 4), sticky="w")

        self.refresh_interval_combo = ttk.Combobox(
            container,
            textvariable=self.refresh_interval_var,
            values=["10", "15", "30", "45", "60", "120", "300", "600"],
            width=8,
        )
        self.refresh_interval_combo.grid(row=row_idx + 3, column=5, padx=(0, 8), sticky="w")
        self.refresh_interval_combo.bind("<KeyRelease>", lambda _e: self._set_profile_custom())
        self.refresh_interval_combo.bind("<<ComboboxSelected>>", lambda _e: self._set_profile_custom())

        self.refresh_unit_combo = ttk.Combobox(
            container,
            textvariable=self.refresh_interval_unit_var,
            values=["Seconds", "Minutes", "Hours"],
            width=10,
            state="readonly",
        )
        self.refresh_unit_combo.grid(row=row_idx + 3, column=6, padx=(0, 8), sticky="w")
        self.refresh_unit_combo.bind("<<ComboboxSelected>>", lambda _e: self._set_profile_custom())

        tk.Label(
            container,
            text="(Allowed: 5 - 3600)",
            bg=self.card,
            fg=self.text_muted,
            font=("Segoe UI", 8),
        ).grid(row=row_idx + 4, column=4, columnspan=3, padx=(2, 0), sticky="w")

        self._load_shift_hours_into_form()
        self._apply_profile_to_interval(set_custom_if_manual=False)
        self._apply_auto_refresh_info_label()

    def _set_profile_custom(self) -> None:
        self.refresh_profile_var.set("Custom")

    def _on_profile_changed(self, _event=None) -> None:
        self._apply_profile_to_interval(set_custom_if_manual=False)

    def _apply_profile_to_interval(self, set_custom_if_manual: bool = True) -> None:
        profile = self.refresh_profile_var.get()
        if profile in self.REFRESH_PROFILES:
            self._set_interval_fields_from_seconds(self.REFRESH_PROFILES[profile])
            return
        if set_custom_if_manual:
            self.refresh_profile_var.set("Custom")

    def _refresh_shift_preview(self, reminders: dict | None = None) -> None:
        if reminders is None:
            reminders = {}
            for shift_name, vars_pair in self.shift_hour_vars.items():
                try:
                    end_raw = vars_pair[1].get().strip()
                    hh, mm = end_raw.split(":")
                    end_minutes = int(hh) * 60 + int(mm)
                    offset_raw = self.shift_reminder_vars.get(shift_name).get().strip() if shift_name in self.shift_reminder_vars else "30"
                    offset_minutes = int(offset_raw)
                    rem_minutes = (end_minutes - offset_minutes) % (24 * 60)
                    reminders[shift_name] = f"{rem_minutes // 60:02d}:{rem_minutes % 60:02d}"
                except (ValueError, AttributeError):
                    reminders[shift_name] = "--:--"

        self.shift_preview_var.set(
            "Reminder preview: "
            f"M {reminders.get('morning', '--:--')} | "
            f"E {reminders.get('evening', '--:--')} | "
            f"N {reminders.get('night', '--:--')}"
        )

    def save_shift_hours(self) -> None:
        self._apply_profile_to_interval(set_custom_if_manual=False)
        try:
            interval_seconds = self._interval_seconds_from_fields()
        except ValueError:
            messagebox.showerror("Invalid Refresh Interval", "Refresh interval value must be an integer.")
            return
        if not (5 <= interval_seconds <= 86400):
            messagebox.showerror("Invalid Refresh Interval", "Refresh interval must be between 5 seconds and 24 hours.")
            return

        self.auto_refresh_ms = interval_seconds * 1000
        if self.refresh_profile_var.get() in self.REFRESH_PROFILES:
            expected = self.REFRESH_PROFILES[self.refresh_profile_var.get()]
            if expected != interval_seconds:
                self.refresh_profile_var.set("Custom")
        self._save_gui_preferences()

        new_hours: dict[str, dict[str, str]] = {}
        for shift_name, vars_pair in self.shift_hour_vars.items():
            start_raw = vars_pair[0].get().strip()
            end_raw = vars_pair[1].get().strip()
            reminder_raw = self.shift_reminder_vars[shift_name].get().strip() if shift_name in self.shift_reminder_vars else "30"
            try:
                start_hh, start_mm = start_raw.split(":")
                end_hh, end_mm = end_raw.split(":")
                start_hour = int(start_hh)
                start_minute = int(start_mm)
                end_hour = int(end_hh)
                end_minute = int(end_mm)
                reminder_minutes = int(reminder_raw)
            except ValueError:
                messagebox.showerror(
                    "Invalid Shift Setting",
                    f"{shift_name.capitalize()} start/end must be HH:MM and reminder must be an integer.",
                )
                return
            if not (
                0 <= start_hour <= 23
                and 0 <= end_hour <= 23
                and 0 <= start_minute <= 59
                and 0 <= end_minute <= 59
            ):
                messagebox.showerror("Invalid Shift Time", f"{shift_name.capitalize()} time must be valid within 00:00 to 23:59.")
                return
            if not (0 <= reminder_minutes <= 720):
                messagebox.showerror("Invalid Reminder", f"{shift_name.capitalize()} reminder must be between 0 and 720 minutes.")
                return
            new_hours[shift_name] = {
                "start_time": f"{start_hour:02d}:{start_minute:02d}",
                "end_time": f"{end_hour:02d}:{end_minute:02d}",
                "reminder_minutes": reminder_minutes,
            }

        try:
            with open(self.shift_settings_file, "w", encoding="utf-8") as f:
                json.dump({"shift_hours": new_hours}, f, indent=2)
        except OSError as e:
            messagebox.showerror("Save Failed", f"Failed to save shift settings: {e}")
            return

        self._refresh_shift_preview()
        self._apply_auto_refresh_info_label()
        self._schedule_next_auto_refresh()
        do_restart = messagebox.askyesno(
            "Shift Hours Saved",
            "Settings saved successfully.\n\nRestart scheduler now to apply new shift schedule?",
        )
        if do_restart:
            self._execute_action(self.restart_script, "Restart")
        else:
            self.refresh_status()

    def reset_settings_to_default(self) -> None:
        defaults = {
            "morning": ("07:00", "15:00", "30"),
            "evening": ("15:00", "23:00", "30"),
            "night": ("23:00", "07:00", "30"),
        }
        for shift_name, values in defaults.items():
            if shift_name in self.shift_hour_vars:
                self.shift_hour_vars[shift_name][0].set(values[0])
                self.shift_hour_vars[shift_name][1].set(values[1])
            if shift_name in self.shift_reminder_vars:
                self.shift_reminder_vars[shift_name].set(values[2])

        self.auto_refresh_var.set(True)
        self.refresh_profile_var.set("Balanced")
        self.refresh_interval_var.set("30")
        self.auto_refresh_ms = 30000

        self._refresh_shift_preview()
        self._apply_auto_refresh_info_label()
        self._save_gui_preferences()

    def _parse_scheduler_state(self, output: str) -> tuple[str, str]:
        text = output.upper()
        if "NOT RUNNING" in text:
            return "NOT RUNNING", "Scheduler process is currently stopped."
        if "RUNNING" in text:
            return "RUNNING", "Scheduler process is active and waiting for reminder times."
        return "UNKNOWN", "Unable to determine scheduler state from monitor output."

    def _fetch_previous_shift_snapshot(self) -> dict:
        code = (
            "import json, sys\n"
            f"sys.path.insert(0, {self.project_root!r})\n"
            "import worklog_reminder as wr\n"
            "order=['morning','evening','night']\n"
            "current=wr.get_auto_shift()\n"
            "previous=order[(order.index(current)-1)%3]\n"
            "tickets=wr.get_inprogress_tickets() or []\n"
            "payload={'current_shift': current, 'previous_shift': previous, 'pending_count': len(tickets), 'tickets': tickets[:25]}\n"
            "print(json.dumps(payload))\n"
        )
        result_code, output = self._run_python_inline(code)
        if result_code != 0:
            return {"error": output or "Failed to pull ticket snapshot."}

        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

        return {"error": output or "No JSON payload returned."}

    def _update_ticket_table(self, tickets: list[dict]) -> None:
        for row_id in self.ticket_table.get_children():
            self.ticket_table.delete(row_id)

        for item in tickets:
            self.ticket_table.insert(
                "",
                tk.END,
                values=(
                    item.get("ticket_id", "N/A"),
                    item.get("title", "N/A"),
                    item.get("assigned_l1", "N/A"),
                    item.get("severity", "N/A"),
                    item.get("status", "N/A"),
                    item.get("last_worklog", "N/A"),
                ),
            )

    def _apply_refresh_payload(self, payload: dict) -> None:
        mon_code = payload["monitor_code"]
        mon_out = payload["monitor_output"]
        scheduler_state, scheduler_msg = payload["scheduler_state"]
        snapshot = payload["snapshot"]

        self._set_output(mon_out if mon_out else "No monitor output.")
        if mon_code != 0:
            self.current_scheduler_state = "UNKNOWN"
            self.scheduler_badge.configure(text="ERROR", bg=self.bad)
            self.scheduler_detail.set("Failed to run monitor script.")
        else:
            self.current_scheduler_state = scheduler_state
            if scheduler_state == "RUNNING":
                self.scheduler_badge.configure(text="RUNNING", bg=self.good)
            elif scheduler_state == "NOT RUNNING":
                self.scheduler_badge.configure(text="STOPPED", bg=self.bad)
            else:
                self.scheduler_badge.configure(text="UNKNOWN", bg=self.warn)
            self.scheduler_detail.set(scheduler_msg)

        self._set_start_stop_button_for_state(self.current_scheduler_state)

        if "error" in snapshot:
            self.prev_shift_badge.configure(text="API ERROR", bg=self.bad)
            self.prev_shift_detail.set("Unable to load previous-shift ticket snapshot from API.")
            self._update_ticket_table([])
            return

        previous_shift = str(snapshot.get("previous_shift", "N/A")).upper()
        pending_count = int(snapshot.get("pending_count", 0))
        tickets = snapshot.get("tickets", [])
        badge_color = self.good if pending_count == 0 else self.warn
        self.prev_shift_badge.configure(text=f"{pending_count} PENDING", bg=badge_color)
        self.prev_shift_detail.set(
            f"Previous shift: {previous_shift}. Showing pending carry-over tickets from API snapshot."
        )
        self._update_ticket_table(tickets)

    def _set_output(self, text: str) -> None:
        self.output.configure(state=tk.NORMAL)
        self.output.delete("1.0", tk.END)
        self.output.insert(tk.END, text)
        self.output.configure(state=tk.DISABLED)

    def _execute_action(self, script_path: str, action_name: str) -> None:
        def worker() -> None:
            self._set_buttons_state(tk.DISABLED)
            code, out = self._run_ps(script_path)
            self._set_output(out if out else f"No output for {action_name}.")
            self._set_buttons_state(tk.NORMAL)
            self.refresh_status()

        threading.Thread(target=worker, daemon=True).start()

    def _set_buttons_state(self, state: str) -> None:
        self.start_stop_btn.configure(state=state)
        self.restart_btn.configure(state=state)
        self.refresh_btn.configure(state=state)
        self.settings_btn.configure(state=state)

    def _apply_auto_refresh_info_label(self) -> None:
        profile = self.refresh_profile_var.get()
        seconds = int(self.auto_refresh_ms / 1000)
        if seconds % 3600 == 0:
            interval_text = f"{seconds // 3600}h"
        elif seconds % 60 == 0:
            interval_text = f"{seconds // 60}m"
        else:
            interval_text = f"{seconds}s"
        if self.auto_refresh_var.get():
            if profile and profile != "Custom":
                self.auto_refresh_info_var.set(
                    f"Auto refresh: ON ({interval_text}, {profile})"
                )
            else:
                self.auto_refresh_info_var.set(f"Auto refresh: ON ({interval_text})")
        else:
            self.auto_refresh_info_var.set("Auto refresh: OFF")

    def _set_start_stop_button_for_state(self, scheduler_state: str) -> None:
        if scheduler_state == "RUNNING":
            self.start_stop_btn.configure(
                text="STOP",
                bg="#7f1d1d",
                activebackground="#991b1b",
            )
        else:
            self.start_stop_btn.configure(
                text="START",
                bg="#0f5132",
                activebackground="#146c43",
            )

    def _schedule_next_auto_refresh(self) -> None:
        if self.auto_refresh_job is not None:
            self.root.after_cancel(self.auto_refresh_job)
            self.auto_refresh_job = None
        if self.auto_refresh_var.get():
            self.auto_refresh_job = self.root.after(self.auto_refresh_ms, self.refresh_status)

    def _toggle_auto_refresh(self) -> None:
        self._save_gui_preferences()
        self._apply_auto_refresh_info_label()
        if self.auto_refresh_var.get():
            self._schedule_next_auto_refresh()
        elif self.auto_refresh_job is not None:
            self.root.after_cancel(self.auto_refresh_job)
            self.auto_refresh_job = None

    def toggle_scheduler(self) -> None:
        if self.current_scheduler_state == "RUNNING":
            self._execute_action(self.stop_script, "Stop")
        else:
            self._execute_action(self.start_script, "Start")

    def restart_scheduler(self) -> None:
        self._execute_action(self.restart_script, "Restart")

    def refresh_status(self) -> None:
        self._set_buttons_state(tk.DISABLED)
        self.scheduler_badge.configure(text="CHECKING...", bg="#334155")
        self.prev_shift_badge.configure(text="LOADING...", bg="#334155")

        def worker() -> None:
            mon_code, mon_out = self._run_ps(self.monitor_script)
            scheduler_state = self._parse_scheduler_state(mon_out)
            snapshot = self._fetch_previous_shift_snapshot()
            payload = {
                "monitor_code": mon_code,
                "monitor_output": mon_out,
                "scheduler_state": scheduler_state,
                "snapshot": snapshot,
            }
            self.root.after(0, lambda: self._on_refresh_done(payload))

        threading.Thread(target=worker, daemon=True).start()

    def _on_refresh_done(self, payload: dict) -> None:
        self._apply_refresh_payload(payload)
        self.last_updated_var.set(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._set_buttons_state(tk.NORMAL)
        self._schedule_next_auto_refresh()


def main() -> None:
    root = tk.Tk()
    app = SchedulerGui(root)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        messagebox.showerror("MESDP Scheduler Control", f"Failed to start GUI: {exc}")
