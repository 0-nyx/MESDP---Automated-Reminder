"""
MESDP Worklog Reminder — API Based
====================================
Send reminder 30 minutes before each shift ends.
Data fetched from ManageEngine SDP MSP via REST API.

Shift Schedule:
  Morning  → 7:00 AM  - 3:00 PM  → Reminder: 2:30 PM
  Evening  → 3:00 PM  - 11:00 PM → Reminder: 10:30 PM
  Night    → 11:00 PM - 7:00 AM  → Reminder: 6:30 AM

Usage:
  python worklog_reminder.py --shift morning          # Run once for morning shift
  python worklog_reminder.py --schedule               # Run scheduler continuously
"""

import argparse
import requests
import json
import smtplib
import urllib3
import sys
import time
import os
from html import escape
try:
    import schedule
except ImportError as e:
    print("❌ 'schedule' library not found. Install with: pip install schedule")
    print(f"   Error: {e}")
    sys.exit(1)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, date
from typing import List, Dict, Tuple, Optional
from config import CONFIG

# Disable SSL warning because SDP uses a self-signed cert on port 8443
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────
SHIFT_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "shift_settings.json")
DEFAULT_SHIFT_HOURS = {
    "morning": (7, 15),    # 7:00 AM - 3:00 PM
    "evening": (15, 23),   # 3:00 PM - 11:00 PM
    "night": (23, 7),      # 11:00 PM - 7:00 AM (crosses midnight)
}
DEFAULT_REMINDER_OFFSETS = {
    "morning": 30,
    "evening": 30,
    "night": 30,
}
REQUEST_TIMEOUT = 10
API_ROW_LIMIT = 100


# ─────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────

def validate_config() -> bool:
    """Validate required config keys."""
    required = {"mesdp": ["base_url", "auth_token"], "email": ["sender", "password", "recipients", "smtp_server", "smtp_port"]}
    for section, keys in required.items():
        if section not in CONFIG:
            print(f"❌ Missing config section: {section}")
            return False
        for key in keys:
            if key not in CONFIG[section]:
                print(f"❌ Missing config: {section}.{key}")
                return False
    if not CONFIG["email"]["recipients"]:
        print("❌ Email recipients list is empty")
        return False
    return True


def execute_reminder(shift_name: str) -> None:
    """Execute reminder for specific shift."""
    if shift_name not in SHIFTS:
        print(f"❌ Invalid shift: {shift_name}")
        return

    shift_info = SHIFTS[shift_name]
    print(f"\n⏰ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running {shift_name} shift reminder...")

    # Pull tickets
    tickets = get_inprogress_tickets()
    if tickets is None:  # API error
        print(f"⚠️  Skipping email send due to API error")
        return

    # Build & send
    subject, html = build_email(shift_info, tickets)
    email_result = send_email(subject, html)

    if email_result:
        print(f"✅ {shift_name.capitalize()} shift reminder sent successfully ({len(tickets)} pending tickets)")
    else:
        print(f"❌ {shift_name.capitalize()} shift reminder FAILED")


def schedule_reminders() -> None:
    """Schedule reminders for all shifts."""
    print(f"\n🕐 Scheduler started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("   Scheduled reminders:")

    for shift, time_str in SHIFT_REMINDER_TIMES.items():
        schedule.every().day.at(time_str).do(execute_reminder, shift_name=shift)
        print(f"   • {shift.capitalize()}: {time_str}")

    print("\n   Press Ctrl+C to stop scheduler\n")

    # Scheduler loop
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)  # Check every second
    except KeyboardInterrupt:
        print("\n\n🛑 Scheduler stopped by user.\n")
        sys.exit(0)


def get_auto_shift() -> str:
    """Auto-detect shift based on current time."""
    current_hour = datetime.now().hour
    for shift, (start, end) in SHIFT_HOURS.items():
        if start > end:  # Shift crosses midnight (like night shift)
            if current_hour >= start or current_hour < end:
                return shift
        else:  # Normal shift within same day
            if start <= current_hour < end:
                return shift
    return "morning"  # Default fallback


def get_sample_tickets() -> List[Dict]:
    """Return local sample tickets for isolated UI testing."""
    return [
        {
            "ticket_id": "#100201",
            "title": "VPN client unable to connect for remote user",
            "assigned_l1": "Aina",
            "severity": "High",
            "status": "In Progress",
            "last_worklog": "No record",
        },
        {
            "ticket_id": "#100245",
            "title": "Printer spooler service intermittently stops",
            "assigned_l1": "Hafiz",
            "severity": "Medium",
            "status": "On Hold",
            "last_worklog": "2026-04-01",
        },
        {
            "ticket_id": "#100299",
            "title": "Email delivery delay on shared mailbox",
            "assigned_l1": "Nurul",
            "severity": "Critical",
            "status": "Pending",
            "last_worklog": "2026-03-31",
        },
    ]


def save_preview_html(file_path: str, html: str) -> bool:
    """Save generated HTML to local file for UI preview."""
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"✅ Preview HTML saved: {file_path}")
        return True
    except OSError as e:
        print(f"❌ Failed to save preview HTML: {e}")
        return False


def _format_hour_label(hour: int) -> str:
    period = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12 or 12
    return f"{hour_12}:00 {period}"


def _format_hhmm_label(hhmm: str) -> str:
    try:
        dt = datetime.strptime(hhmm, "%H:%M")
        return dt.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return hhmm


def _parse_hhmm(value: str) -> Optional[Tuple[int, int]]:
    try:
        hour_str, minute_str = value.split(":")
        hour = int(hour_str)
        minute = int(minute_str)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        return None
    return None


def _compute_reminder_time(end_hour: int) -> str:
    total_minutes = (end_hour * 60 - 30) % (24 * 60)
    hh = total_minutes // 60
    mm = total_minutes % 60
    return f"{hh:02d}:{mm:02d}"


def _to_minutes(hhmm: str) -> Optional[int]:
    parsed = _parse_hhmm(hhmm)
    if not parsed:
        return None
    hh, mm = parsed
    return hh * 60 + mm


def _build_shift_metadata(
    shift_hours: Dict[str, Tuple[int, int]],
    shift_times: Dict[str, Tuple[str, str]],
    reminder_offsets: Dict[str, int],
) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    reminder_times: Dict[str, str] = {}
    shift_defs: Dict[str, Dict[str, str]] = {}

    for shift_name in ("morning", "evening", "night"):
        start_hour, end_hour = shift_hours[shift_name]
        start_hhmm, end_hhmm = shift_times[shift_name]

        end_minutes = _to_minutes(end_hhmm)
        offset_minutes = int(reminder_offsets.get(shift_name, 30))
        if offset_minutes < 0:
            offset_minutes = 0
        if offset_minutes > 720:
            offset_minutes = 720

        if end_minutes is None:
            reminder_24h = _compute_reminder_time(end_hour)
        else:
            reminder_minutes = (end_minutes - offset_minutes) % (24 * 60)
            reminder_24h = f"{reminder_minutes // 60:02d}:{reminder_minutes % 60:02d}"

        reminder_dt = datetime.strptime(reminder_24h, "%H:%M")
        reminder_12h = reminder_dt.strftime("%I:%M %p").lstrip("0")
        label = f"{shift_name.capitalize()} Shift ({_format_hhmm_label(start_hhmm)} – {_format_hhmm_label(end_hhmm)})"

        reminder_times[shift_name] = reminder_24h
        shift_defs[shift_name] = {
            "label": label,
            "reminder": reminder_12h,
        }

    return reminder_times, shift_defs


def load_shift_hours() -> Tuple[Dict[str, Tuple[int, int]], Dict[str, Tuple[str, str]], Dict[str, int]]:
    """Load shift hours/times/reminder offsets from JSON settings file with fallback to defaults."""
    shift_hours = dict(DEFAULT_SHIFT_HOURS)
    shift_times = {
        name: (f"{vals[0]:02d}:00", f"{vals[1]:02d}:00")
        for name, vals in shift_hours.items()
    }
    reminder_offsets = dict(DEFAULT_REMINDER_OFFSETS)
    if not os.path.exists(SHIFT_SETTINGS_FILE):
        return shift_hours, shift_times, reminder_offsets

    try:
        with open(SHIFT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠️ Failed to read shift settings file: {e}. Using default shift hours.")
        return shift_hours, shift_times, reminder_offsets

    raw_hours = payload.get("shift_hours", {}) if isinstance(payload, dict) else {}
    for shift_name in ("morning", "evening", "night"):
        raw_shift = raw_hours.get(shift_name, {}) if isinstance(raw_hours, dict) else {}
        if not isinstance(raw_shift, dict):
            continue

        # New format: start_time/end_time (HH:MM)
        start_time = raw_shift.get("start_time")
        end_time = raw_shift.get("end_time")
        parsed_start = _parse_hhmm(start_time) if isinstance(start_time, str) else None
        parsed_end = _parse_hhmm(end_time) if isinstance(end_time, str) else None
        if parsed_start and parsed_end:
            shift_hours[shift_name] = (parsed_start[0], parsed_end[0])
            shift_times[shift_name] = (f"{parsed_start[0]:02d}:{parsed_start[1]:02d}", f"{parsed_end[0]:02d}:{parsed_end[1]:02d}")
            continue

        # Backward-compatible format: start/end integers
        start = raw_shift.get("start")
        end = raw_shift.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start <= 23 and 0 <= end <= 23:
            shift_hours[shift_name] = (start, end)
            shift_times[shift_name] = (f"{start:02d}:00", f"{end:02d}:00")

        raw_offset = raw_shift.get("reminder_minutes")
        if isinstance(raw_offset, int) and 0 <= raw_offset <= 720:
            reminder_offsets[shift_name] = raw_offset

    return shift_hours, shift_times, reminder_offsets

# ─────────────────────────────────────────
# SHIFT DEFINITIONS
# ─────────────────────────────────────────

SHIFT_HOURS, SHIFT_TIMES, SHIFT_REMINDER_OFFSETS = load_shift_hours()
SHIFT_REMINDER_TIMES, SHIFTS = _build_shift_metadata(SHIFT_HOURS, SHIFT_TIMES, SHIFT_REMINDER_OFFSETS)


# ─────────────────────────────────────────
# PULL DATA FROM MESDP API
# ─────────────────────────────────────────

def get_inprogress_summary() -> Optional[Dict]:
    """Fetch in-progress ticket summary including pending and updated-today counts."""
    base_url   = CONFIG["mesdp"]["base_url"]
    auth_token = CONFIG["mesdp"]["auth_token"]

    url = f"{base_url}/api/v3/requests"

    input_data = {
        "list_info": {
            "row_count": API_ROW_LIMIT,
            "start_index": 1,
            "sort_field": "id",
            "sort_order": "asc",
            "search_criteria": [
                {
                    "field": "status.name",
                    "condition": "is",
                    "value": "In Progress"
                }
            ]
        }
    }

    headers = {
        "authtoken": auth_token,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params={"input_data": json.dumps(input_data)},
            verify=False,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            print(f"❌ API Error {response.status_code}: {response.text}")
            return []
    except requests.exceptions.Timeout:
        print("❌ API Timeout: MESDP service took too long to respond")
        return []
    except requests.exceptions.RequestException as e:
        print(f"❌ API Connection Error: {e}")
        return []

    data               = response.json()
    requests_list      = data.get("requests", [])
    today              = date.today()
    pending_tickets    = []
    updated_today_count = 0
    total_considered   = 0

    for req in requests_list:
        req_id = req.get("id")

        # Prefer severity from the Priority section, with fallback to Level.
        priority_data = req.get("priority") or {}
        if isinstance(priority_data, dict):
            severity = priority_data.get("name") or priority_data.get("value") or priority_data.get("display_value") or "N/A"
        elif isinstance(priority_data, str):
            severity = priority_data
        else:
            severity = "N/A"

        if severity == "N/A":
            level_data = req.get("level") or {}
            if isinstance(level_data, dict):
                severity = level_data.get("name") or level_data.get("value") or "N/A"
            elif isinstance(level_data, str):
                severity = level_data

        # Exclude tickets with severity PM from counts and table.
        if str(severity).strip().upper() == "PM":
            continue

        total_considered += 1
        last_worklog = get_last_worklog_date(req_id, auth_token, base_url)
        technician = req.get("technician") or {}

        status_data = req.get("status") or {}
        if isinstance(status_data, dict):
            ticket_status = status_data.get("name") or status_data.get("value") or status_data.get("display_value") or "In Progress"
        elif isinstance(status_data, str):
            ticket_status = status_data
        else:
            ticket_status = "In Progress"

        if last_worklog is not None and last_worklog >= today:
            updated_today_count += 1
            continue

        pending_tickets.append({
            "ticket_id": f"#{req_id}",
            "title": req.get("subject", "No Subject"),
            "assigned_l1": technician.get("name", "Unassigned"),
            "severity": severity if severity else "N/A",
            "status": ticket_status,
            "last_worklog": str(last_worklog) if last_worklog else "No record",
        })

    return {
        "tickets": pending_tickets,
        "pending_count": len(pending_tickets),
        "updated_today_count": updated_today_count,
        "total_in_progress": total_considered,
    }


def get_inprogress_tickets() -> Optional[List[Dict]]:
    """Fetch pending 'In Progress' tickets (worklog not updated today)."""
    summary = get_inprogress_summary()
    if summary is None:
        return []
    return summary.get("tickets", [])


def get_last_worklog_date(request_id: int, auth_token: str, base_url: str) -> Optional[date]:
    """Get latest worklog date for a specific ticket."""
    url     = f"{base_url}/api/v3/requests/{request_id}/worklogs"
    headers = {"authtoken": auth_token}

    try:
        response = requests.get(url, headers=headers, verify=False, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None

        worklogs = response.json().get("worklogs", [])
        if not worklogs:
            return None

        # Ambil worklog paling recent dengan safe type conversion
        def get_timestamp(w):
            try:
                val = w.get("start_time", {}).get("value", 0)
                return int(val) if val else 0
            except (ValueError, TypeError):
                return 0

        latest = max(worklogs, key=get_timestamp)
        timestamp_ms = get_timestamp(latest)

        if timestamp_ms:
            return datetime.fromtimestamp(timestamp_ms / 1000).date()

    except requests.exceptions.Timeout:
        print(f"⚠️ Worklog API timeout for #{request_id}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Worklog API error for #{request_id}: {e}")
    except Exception as e:
        print(f"⚠️ Worklog check error for #{request_id}: {e}")

    return None


# ─────────────────────────────────────────
# BUILD MESSAGES
# ─────────────────────────────────────────

def build_email(shift_info: Dict, tickets: List[Dict]) -> Tuple[str, str]:
    """Build HTML email body with modern design."""
    today_str   = datetime.now().strftime("%d %B %Y")
    shift_label = shift_info["label"]

    if not tickets:
        status_block = """
        <div style="background:#e8f5e9; border-left:4px solid #4caf50; padding:16px; margin:20px 0; border-radius:6px; text-align:center;">
            <p style="color:#2e7d32; font-size:16px; margin:0;"><strong>✅ All assigned tickets have up-to-date worklogs for today.</strong> Thank you for maintaining excellent handover quality.</p>
        </div>
        """
    else:
        rows = ""
        for idx, t in enumerate(tickets):
            row_bg = "#f8fafc" if idx % 2 == 0 else "#ffffff"
            severity = escape(str(t.get("severity", "N/A")))
            ticket_status = escape(str(t.get("status", "In Progress")))
            severity_level = severity.upper()
            severity_style = "background:#eaf7ee; color:#1d6f42;"
            if severity_level in {"CRITICAL", "HIGH", "P1", "P2"}:
                severity_style = "background:#fdecec; color:#a12622;"
            elif severity_level in {"MEDIUM", "MODERATE", "P3"}:
                severity_style = "background:#fff4e5; color:#8a6100;"

            status_level = ticket_status.upper()
            status_style = "background:#eaf3fb; color:#123f63;"
            if status_level in {"ON HOLD", "PENDING", "WAITING FOR USER"}:
                status_style = "background:#fff4e5; color:#8a6100;"
            elif status_level in {"RESOLVED", "CLOSED"}:
                status_style = "background:#eaf7ee; color:#1d6f42;"

            rows += f"""
            <tr style="background:{row_bg};">
                <td style="padding:12px; border-bottom:1px solid #e5e7eb; font-weight:700; color:#0f4c81; white-space:nowrap; text-align:center;">{escape(str(t['ticket_id']))}</td>
                <td style="padding:12px; border-bottom:1px solid #e5e7eb; text-align:center; line-height:1.4;">{escape(str(t['title']))}</td>
                <td style="padding:12px; border-bottom:1px solid #e5e7eb; text-align:center;">{escape(str(t['assigned_l1']))}</td>
                <td style="padding:12px; border-bottom:1px solid #e5e7eb; text-align:center; font-weight:700;">
                    <span style="display:inline-block; padding:4px 10px; border-radius:999px; font-size:12px; white-space:nowrap; {severity_style}">{severity}</span>
                </td>
                <td style="padding:12px; border-bottom:1px solid #e5e7eb; text-align:center; font-weight:700;">
                    <span style="display:inline-block; padding:4px 10px; border-radius:999px; font-size:12px; white-space:nowrap; {status_style}">{ticket_status}</span>
                </td>
                <td style="padding:12px; border-bottom:1px solid #e5e7eb; color:#7a1c1c; font-weight:700; text-align:center; white-space:nowrap;">{escape(str(t['last_worklog']))}</td>
            </tr>"""

        status_block = f"""
        <div style="margin:22px 0 10px; text-align:left;">
            <p style="font-size:16px; color:#a12622; margin:0 0 12px; font-weight:700;">⚠️ {len(tickets)} Ticket(s) Pending Worklog Update</p>
            <div style="background:#fff7ed; border-left:4px solid #f59e0b; padding:12px; margin-bottom:16px; border-radius:6px;">
                <p style="margin:0; color:#9a3412; font-size:14px; line-height:1.4;">The tickets below are missing today\'s worklog update. Please update them before shift end to ensure a complete and accurate handover.</p>
            </div>
            <table role="presentation" style="border-collapse:separate; border-spacing:0; width:100%; font-size:13px; background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; overflow:hidden; table-layout:fixed;">
                <thead>
                    <tr style="background:#0f4c81; color:#ffffff; font-weight:700;">
                        <th style="padding:12px; border-bottom:1px solid #d1d5db; text-align:center; width:12%;">Ticket ID</th>
                        <th style="padding:12px; border-bottom:1px solid #d1d5db; text-align:center; width:31%;">Title</th>
                        <th style="padding:12px; border-bottom:1px solid #d1d5db; text-align:center; width:18%;">Assigned L1</th>
                        <th style="padding:12px; border-bottom:1px solid #d1d5db; text-align:center; width:12%;">Severity</th>
                        <th style="padding:12px; border-bottom:1px solid #d1d5db; text-align:center; width:13%;">Status</th>
                        <th style="padding:12px; border-bottom:1px solid #d1d5db; text-align:center; width:14%;">Last Updated</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>
        """

    html = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="font-family:'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif; color:#111827; margin:0; padding:0; background:#eef2f7;">
        <div style="max-width:780px; margin:24px auto; padding:0 12px;">
            <!-- Header -->
            <div style="background:linear-gradient(135deg, #0f4c81 0%, #1769aa 100%); padding:28px 22px; text-align:center; border-radius:10px 10px 0 0;">
                <h1 style="color:white; margin:0 0 8px; font-size:26px; font-weight:bold;">🔔 CLL MESDP Worklog Update Reminder</h1>
                <p style="color:white; margin:0; font-size:14px;">{shift_label}</p>
                <p style="color:rgba(255,255,255,0.9); margin:8px 0 0; font-size:12px;">📅 {today_str}</p>
            </div>

            <!-- Content -->
            <div style="background:white; padding:28px; border-radius:0 0 10px 10px; box-shadow:0 8px 20px rgba(15, 23, 42, 0.08);">
                <p style="margin:0 0 16px; font-size:16px; color:#1f2937; text-align:left;">Dear L1 Team,</p>
                
                <div style="background:#eaf3fb; border-left:4px solid #1769aa; padding:12px; margin-bottom:20px; border-radius:6px; text-align:left;">
                    <p style="margin:0; color:#123f63; font-size:14px; line-height:1.45;"><strong>⏰ Action required:</strong> Update all assigned tickets with the latest worklog details, current status, and next action before the shift ends.</p>
                </div>

                {status_block}

                <!-- Footer -->
                <div style="background:#f8fafc; border-top:1px solid #e5e7eb; margin-top:24px; padding:16px; border-radius:6px; text-align:center;">
                    <p style="margin:0; font-size:12px; color:#666;">
                        ⚙️ This is an automated notification from the CLL MESDP Worklog Update Reminder.<br>
                        Please do not reply to this email.
                    </p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    subject = f"[MESDP] CLL Worklog Update Reminder — {shift_label.split('(')[0].strip()} | {today_str}"
    return subject, html


def build_teams_message(shift_info: Dict, tickets: List[Dict]) -> Dict:
    """Build Teams Adaptive Card with modern design."""
    today_str   = datetime.now().strftime("%d %B %Y")
    shift_label = shift_info["label"]
    shift_name  = shift_label.split("(")[0].strip()

    if not tickets:
        fact_items = [{"name": "Status", "value": "✅ All Tickets Updated"}]
        summary_head = "Excellent Work!"
        summary_msg = "All tickets have updated their work logs today. Great performance! 🎉"
        summary_color = "Good"
        section_title = "Status"
    else:
        fact_items = [
            {"name": f"{t['ticket_id']} [{t['severity']}]", "value": f"{t['title']} — 👤 {t['assigned_l1']}"}
            for t in tickets[:10]  # Show max 10 tickets
        ]
        summary_head = f"⚠️ {len(tickets)} Pending Ticket(s)"
        summary_msg = f"Please update {len(tickets)} ticket(s) before shift ends."
        summary_color = "Warning"
        section_title = "Tickets Needing Updates"

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "Container",
                        "style": "accent",
                        "items": [
                            {
                                "type": "ColumnSet",
                                "columns": [
                                    {
                                        "width": "stretch",
                                        "items": [
                                            {
                                                "type": "TextBlock",
                                                "text": "🔔 CLL MESDP Worklog Update Reminder",
                                                "size": "Large",
                                                "weight": "Bolder",
                                                "color": "Light",
                                                "horizontalAlignment": "Center"
                                            },
                                            {
                                                "type": "TextBlock",
                                                "text": f"{shift_name} Shift",
                                                "size": "Medium",
                                                "color": "Light",
                                                "spacing": "Small",
                                                "horizontalAlignment": "Center"
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "type": "Container",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": summary_head,
                                "size": "Medium",
                                "weight": "Bolder",
                                "color": summary_color,
                                "horizontalAlignment": "Center"
                            },
                            {
                                "type": "TextBlock",
                                "text": summary_msg,
                                "wrap": True,
                                "size": "Small",
                                "color": "Dark",
                                "spacing": "Small",
                                "horizontalAlignment": "Center"
                            }
                        ],
                        "spacing": "Medium"
                    }
                ]
                + [
                    {
                        "type": "Container",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": section_title,
                                "weight": "Bolder",
                                "size": "Small",
                                "color": "Attention" if tickets else "Default",
                                "horizontalAlignment": "Center"
                            },
                            {
                                "type": "FactSet",
                                "facts": fact_items,
                                "spacing": "Small"
                            }
                        ],
                        "separator": True,
                        "spacing": "Medium"
                    },
                    {
                        "type": "Container",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "📅 " + today_str,
                                "size": "Small",
                                "color": "Subtle",
                                "horizontalAlignment": "Center"
                            },
                            {
                                "type": "TextBlock",
                                "text": "Please update work logs before shift ends. Thank you! 🙏",
                                "wrap": True,
                                "size": "Small",
                                "color": "Accent",
                                "spacing": "Small",
                                "horizontalAlignment": "Center"
                            }
                        ],
                        "separator": True,
                        "spacing": "Medium"
                    }
                ]
            }
        }]
    }
    return payload


# ─────────────────────────────────────────
# SEND FUNCTIONS
# ─────────────────────────────────────────

def send_email(subject: str, html_body: str) -> bool:
    """Send email dengan error handling."""
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = CONFIG["email"]["sender"]
        msg["To"]      = ", ".join(CONFIG["email"]["recipients"])
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(CONFIG["email"]["smtp_server"], CONFIG["email"]["smtp_port"], timeout=REQUEST_TIMEOUT) as server:
            if CONFIG["email"].get("use_starttls", True):
                server.starttls()
            server.login(CONFIG["email"]["sender"], CONFIG["email"]["password"])
            server.sendmail(CONFIG["email"]["sender"], CONFIG["email"]["recipients"], msg.as_string())

        print("✅ Email sent successfully.")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ SMTP Auth Failed: Verify credentials/app password or SMTP AUTH policy")
        print(f"   Error: {e}")
        return False
    except smtplib.SMTPException as e:
        print(f"❌ SMTP Error: {e}")
        return False
    except Exception as e:
        print(f"❌ Email Error: {e}")
        return False


def send_teams(payload: Dict) -> bool:
    """Send Teams message dengan error handling."""
    try:
        resp = requests.post(
            CONFIG["teams"]["webhook_url"],
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT
        )
        if resp.status_code == 200:
            print("✅ Teams message sent.")
            return True
        else:
            print(f"❌ Teams error {resp.status_code}")
            return False
    except Exception as e:
        print(f"❌ Teams error: {e}")
        return False


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def main():
    """Main workflow."""
    # Validate config
    if not validate_config():
        print("\n❌ Config validation failed. Exiting.\n")
        sys.exit(1)

    # Parse arguments
    parser = argparse.ArgumentParser(description="MESDP Worklog Reminder")
    parser.add_argument("--shift", choices=["morning", "evening", "night"], 
                        help="Shift to send reminder (morning/evening/night)")
    parser.add_argument("--schedule", action="store_true", 
                        help="Run scheduler mode (continuous, auto-send at reminder times)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build output without sending email/Teams")
    parser.add_argument("--sandbox", action="store_true",
                        help="Isolated mode: skip API and sending, use local sample tickets")
    parser.add_argument("--preview-file", default="preview_worklog_email.html",
                        help="Path to save generated HTML preview (default: preview_worklog_email.html)")
    args = parser.parse_args()

    # Scheduler mode
    if args.schedule:
        schedule_reminders()
        return

    # Single run mode
    auto_shift = get_auto_shift() if not args.shift else args.shift
    shift_info = SHIFTS[auto_shift]

    print(f"\n{'='*55}")
    print(f"  MESDP Worklog Reminder — {shift_info['label']}")
    print(f"  Run time: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")

    # Pull tickets
    if args.sandbox:
        print("🧪 Sandbox mode enabled: using local sample tickets (no API call).")
        tickets = get_sample_tickets()
    else:
        print("📡 Connecting to MESDP API...")
        tickets = get_inprogress_tickets()

    if tickets is None:
        print("❌ Failed to pull tickets. Exiting.")
        sys.exit(1)

    print(f"⚠️  {len(tickets)} ticket(s) have not updated worklog today.")

    # Build & send
    subject, html = build_email(shift_info, tickets)
    preview_result = save_preview_html(args.preview_file, html)

    if args.sandbox:
        email_result = False
        teams_result = False
        email_status_text = "⏸️  Skipped (sandbox)"
        print("⏸️  Sandbox mode: email sending skipped.")
    elif args.dry_run:
        email_result = False
        teams_result = False
        email_status_text = "⏸️  Skipped (dry-run)"
        print("⏸️  Dry-run mode: email sending skipped.")
    else:
        email_result = send_email(subject, html)
        teams_result = False  # Teams disabled
        email_status_text = "✅ Yes" if email_result else "❌ No"

    # Status report
    print("\n" + "="*55)
    print("  📊 EXECUTION SUMMARY")
    print(f"  Shift: {shift_info['label']}")
    print(f"  Tickets pending: {len(tickets)}")
    print(f"  Preview generated: {'✅ Yes' if preview_result else '❌ No'}")
    print(f"  Email sent: {email_status_text}")
    print(f"  Teams sent: {'✅ Yes' if teams_result else '⏸️  Disabled'}")
    print("="*55 + "\n")

    if args.sandbox or args.dry_run:
        result_code = 0 if preview_result else 1
    else:
        result_code = 0 if email_result else 1
    sys.exit(result_code)


if __name__ == "__main__":
    main()
