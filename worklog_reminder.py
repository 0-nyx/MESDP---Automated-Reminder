"""
MESDP Worklog Reminder — API Based
====================================
Send reminder 1 hour before each shift ends.
Data fetched from ManageEngine SDP MSP via REST API.
Who is on shift comes from the CST-NOC roster Excel file (see roster.py).

Shift Schedule (defaults, editable in the GUI; matches the 12-hour roster):
  Morning  → 8:00 AM - 8:00 PM → Reminder: 7:00 PM
  Night    → 8:00 PM - 8:00 AM → Reminder: 7:00 AM

Usage:
  python worklog_reminder.py --shift morning          # Run once for morning shift
  python worklog_reminder.py --schedule               # Run scheduler continuously
"""

import argparse
import requests
import json
import smtplib
import ssl
import sys
import time
import os
from email.utils import parseaddr
from html import escape
try:
    import schedule
except ImportError as e:
    print("[ERROR] 'schedule' library not found. Install with: pip install schedule")
    print(f"   Error: {e}")
    sys.exit(1)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import re
from datetime import datetime, date, timedelta
from typing import List, Dict, Tuple, Optional
from config import CONFIG

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (OSError, ValueError):
            pass

# Disable SSL warning because SDP uses a self-signed cert on port 8443

# ─────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────
def _resolve_shift_settings_file() -> str:
    """The GUI saves shift hours per user under %APPDATA%; prefer that copy and
    fall back to the file shipped next to this script."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        user_file = os.path.join(appdata, "CLL MESDP", "shift_settings.json")
        if os.path.exists(user_file):
            return user_file
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "shift_settings.json")


SHIFT_SETTINGS_FILE = _resolve_shift_settings_file()
SHIFT_ORDER = ("morning", "night")   # same two shifts as the CST-NOC roster
DEFAULT_SHIFT_HOURS = {
    "morning": (8, 20),    # 8:00 AM - 8:00 PM
    "night": (20, 8),      # 8:00 PM - 8:00 AM (crosses midnight)
}
# Minutes before shift end: a first reminder at 1 hour, a final one at 30 min.
DEFAULT_REMINDER_OFFSETS = {
    "morning": [60, 30],
    "night": [60, 30],
}
REQUEST_TIMEOUT = 10
API_ROW_LIMIT = 100
WORKLOG_ROW_LIMIT = 100


# ─────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────

def validate_config() -> bool:
    """Validate required config keys."""
    required = {"mesdp": ["base_url", "auth_token"], "email": ["sender", "password", "recipients", "smtp_server", "smtp_port"]}
    for section, keys in required.items():
        if section not in CONFIG:
            print(f"[ERROR] Missing config section: {section}")
            return False
        for key in keys:
            if key not in CONFIG[section]:
                print(f"[ERROR] Missing config: {section}.{key}")
                return False
    if not CONFIG["email"]["recipients"]:
        print("[ERROR] Email recipients list is empty")
        return False
    return True


def execute_reminder(shift_name: str, final: bool = False) -> None:
    """Execute reminder for specific shift. `final` marks the last reminder of the shift."""
    if shift_name not in SHIFTS:
        print(f"[ERROR] Invalid shift: {shift_name}")
        return

    shift_info = SHIFTS[shift_name]
    kind = "final" if final else "first"
    print(f"\n[RUN] [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running {shift_name} shift reminder ({kind})...")

    # Pull tickets with no worklog since this shift's cutoff
    tickets = get_inprogress_tickets(worklog_cutoff(shift_name))
    if tickets is None:  # API error
        print(f"[WARN] Skipping reminder due to API error")
        send_api_alert(shift_info, final)
        return

    # Build & send
    subject, html = build_email(shift_info, tickets, get_shift_roster(shift_name),
                                get_shift_activity(shift_name), shift_name, final=final)
    email_result = send_email(subject, html)

    if email_result:
        print(f"[OK] {shift_name.capitalize()} shift reminder sent successfully ({len(tickets)} pending tickets)")
    else:
        print(f"[ERROR] {shift_name.capitalize()} shift reminder FAILED")


def schedule_reminders() -> None:
    """Schedule reminders for all shifts."""
    print(f"\nScheduler started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("   Scheduled reminders:")

    for shift, times in SHIFT_REMINDER_TIMES.items():
        for i, time_str in enumerate(times):
            final = len(times) > 1 and i == len(times) - 1
            schedule.every().day.at(time_str).do(execute_reminder, shift_name=shift, final=final)
            print(f"   - {shift.capitalize()}: {time_str}{' (final)' if final else ''}")

    print("\n   Press Ctrl+C to stop scheduler\n")

    # Scheduler loop
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)  # Check every second
    except KeyboardInterrupt:
        print("\n\nScheduler stopped by user.\n")
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
        print(f"[OK] Preview HTML saved: {file_path}")
        return True
    except OSError as e:
        print(f"[ERROR] Failed to save preview HTML: {e}")
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


def parse_reminder_offsets(raw) -> Optional[List[int]]:
    """Minutes-before-shift-end for each reminder, largest first.

    Accepts an int (old format), a list of ints, or text such as "60, 30".
    Returns None if nothing valid is found."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        items = [raw]
    elif isinstance(raw, str):
        items = [p for p in re.split(r"[,\s;]+", raw.strip()) if p]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return None
    offsets = set()
    for item in items:
        try:
            value = int(item)
        except (TypeError, ValueError):
            return None
        if not 0 <= value <= 720:
            return None
        offsets.add(value)
    return sorted(offsets, reverse=True) or None


def _build_shift_metadata(
    shift_hours: Dict[str, Tuple[int, int]],
    shift_times: Dict[str, Tuple[str, str]],
    reminder_offsets: Dict[str, List[int]],
) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, str]]]:
    """Reminder clock times per shift (one per offset, earliest first) and labels."""
    reminder_times: Dict[str, List[str]] = {}
    shift_defs: Dict[str, Dict[str, str]] = {}

    for shift_name in SHIFT_ORDER:
        start_hour, end_hour = shift_hours[shift_name]
        start_hhmm, end_hhmm = shift_times[shift_name]
        end_minutes = _to_minutes(end_hhmm)
        if end_minutes is None:
            end_minutes = end_hour * 60

        times_24h = []
        for offset in reminder_offsets.get(shift_name) or DEFAULT_REMINDER_OFFSETS[shift_name]:
            minutes = (end_minutes - offset) % (24 * 60)
            times_24h.append(f"{minutes // 60:02d}:{minutes % 60:02d}")

        label = f"{shift_name.capitalize()} Shift ({_format_hhmm_label(start_hhmm)} – {_format_hhmm_label(end_hhmm)})"
        reminder_times[shift_name] = times_24h
        shift_defs[shift_name] = {
            "label": label,
            "reminder": ", ".join(datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0") for t in times_24h),
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
        print(f"[WARN] Failed to read shift settings file: {e}. Using default shift hours.")
        return shift_hours, shift_times, reminder_offsets

    raw_hours = payload.get("shift_hours", {}) if isinstance(payload, dict) else {}
    if isinstance(raw_hours, dict) and "evening" in raw_hours:
        # Old 3-shift (8-hour) settings: those hours no longer match the
        # 12-hour roster, so fall back to the defaults until the GUI saves anew.
        print("[WARN] Shift settings are in the old 3-shift format; using default 2-shift hours "
              "(08:00-20:00 / 20:00-08:00). Re-save shift settings in the GUI to customise.")
        return shift_hours, shift_times, reminder_offsets

    for shift_name in SHIFT_ORDER:
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
        else:
            # Backward-compatible format: start/end integers
            start = raw_shift.get("start")
            end = raw_shift.get("end")
            if isinstance(start, int) and isinstance(end, int) and 0 <= start <= 23 and 0 <= end <= 23:
                shift_hours[shift_name] = (start, end)
                shift_times[shift_name] = (f"{start:02d}:00", f"{end:02d}:00")

        offsets = parse_reminder_offsets(raw_shift.get("reminder_minutes"))
        if offsets:
            reminder_offsets[shift_name] = offsets

    return shift_hours, shift_times, reminder_offsets

# ─────────────────────────────────────────
# SHIFT DEFINITIONS
# ─────────────────────────────────────────

SHIFT_HOURS, SHIFT_TIMES, SHIFT_REMINDER_OFFSETS = load_shift_hours()
SHIFT_REMINDER_TIMES, SHIFTS = _build_shift_metadata(SHIFT_HOURS, SHIFT_TIMES, SHIFT_REMINDER_OFFSETS)


# ─────────────────────────────────────────
# PULL DATA FROM MESDP API
# ─────────────────────────────────────────

def ticket_severity(req: Dict) -> str:
    """Severity from the Priority field, falling back to Level."""
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
    return severity


def is_pm_ticket(req: Dict) -> bool:
    """Preventive-maintenance tickets are left out of every reminder count."""
    return str(ticket_severity(req)).strip().upper() == "PM"


# Details of the most recent MESDP API failure, used for the alert email.
_last_api_error: Dict = {}


def _set_api_error(detail: str, status_code: Optional[int] = None) -> None:
    # MESDP reports a bad token as HTTP 400 with a 401 inside the JSON body,
    # e.g. "AuthToken in the request is invalid. Unable to authenticate."
    detail = str(detail)
    auth_failed = status_code in (401, 403) or bool(
        re.search(r'"status_code"\s*:\s*40[13]\b|authtoken|unable to authenticate', detail, re.I))
    _last_api_error.clear()
    _last_api_error.update({"detail": detail[:400], "status_code": status_code,
                            "auth_failed": auth_failed})


def build_api_alert_email(shift_info: Dict, final: bool = False) -> Tuple[str, str]:
    """Alert sent instead of the reminder when MESDP data could not be pulled."""
    detail = _last_api_error.get("detail") or "Unknown error (see scheduler log)"
    token_problem = bool(_last_api_error.get("auth_failed"))
    when = datetime.now().strftime("%d %b %Y %H:%M")
    kind = "final reminder" if final else "reminder"

    if token_problem:
        cause = "The MESDP API token was rejected. It has most likely expired or been changed."
        action = ("Generate a new API token in MESDP, then open <b>MESDP Scheduler Control</b> &rarr; "
                  "<b>App Configuration</b>, paste it into <b>MESDP Token</b> and click <b>SAVE CONFIG</b>. "
                  "Leave the other fields as they are.")
    else:
        cause = "The scheduler could not get ticket data from MESDP (network, server or API problem)."
        action = ("Check that MESDP is reachable from this PC. If MESDP is up, the API token may need "
                  "updating in <b>App Configuration</b>.")

    subject = (f"[ACTION NEEDED] MESDP API error: {shift_info['label']} {kind} not sent"
               .replace("\r", "").replace("\n", " "))
    html = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;color:#1f2937;">
<div style="max-width:640px;border:1px solid #f3c7a8;border-radius:8px;padding:20px;background:#fff8f3;">
  <h2 style="margin:0 0 8px;color:#b54708;">Worklog {escape(kind)} not sent</h2>
  <p style="margin:0 0 12px;">{escape(shift_info['label'])} &middot; {escape(when)}</p>
  <p style="margin:0 0 12px;"><b>What happened:</b> {escape(cause)}</p>
  <p style="margin:0 0 12px;"><b>Impact:</b> The team did not get this {escape(kind)}, so they do not have an
     up-to-date list of In Progress tickets that still need a worklog before the shift ends.
     Fix this before the next reminder so the team still gets the list in time.</p>
  <p style="margin:0 0 12px;"><b>What to do:</b> {action}</p>
  <p style="margin:0 0 4px;color:#6b7280;font-size:12px;">Error detail:</p>
  <pre style="margin:0;white-space:pre-wrap;font-size:12px;background:#fff;border:1px solid #e5e7eb;padding:8px;border-radius:4px;">{escape(detail)}</pre>
  <p style="margin:12px 0 0;color:#6b7280;font-size:12px;">The scheduler keeps running and will try again at the next reminder time.</p>
</div></body></html>"""
    return subject, html


def send_api_alert(shift_info: Dict, final: bool = False) -> bool:
    subject, html = build_api_alert_email(shift_info, final)
    print("[ALERT] Sending MESDP API error alert email...")
    return send_email(subject, html)


def get_inprogress_summary(cutoff: Optional[datetime] = None) -> Optional[Dict]:
    """Fetch in-progress ticket summary.

    A ticket counts as updated when its latest worklog is at or after `cutoff`
    (see worklog_cutoff(); defaults to midnight today)."""
    base_url   = CONFIG["mesdp"]["base_url"]
    auth_token = CONFIG["mesdp"]["auth_token"]

    url = f"{base_url}/api/v3/requests"
    headers = {
        "authtoken": auth_token,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    all_requests = []
    start_index = 1
    while True:
        input_data = {
            "list_info": {
                "row_count": API_ROW_LIMIT,
                "start_index": start_index,
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

        try:
            response = requests.get(
                url,
                headers=headers,
                params={"input_data": json.dumps(input_data)},
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                print(f"[ERROR] API Error {response.status_code}: {response.text}")
                _set_api_error(f"HTTP {response.status_code}: {response.text}",
                               status_code=response.status_code)
                return None
        except requests.exceptions.Timeout:
            print("[ERROR] API Timeout: MESDP service took too long to respond")
            _set_api_error("Timeout: MESDP took too long to respond")
            return None
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] API Connection Error: {e}")
            _set_api_error(f"Connection error: {e}")
            return None

        try:
            data = response.json()
        except ValueError as e:
            print(f"[ERROR] API returned invalid JSON: {e}")
            _set_api_error(f"Invalid JSON from MESDP: {e}")
            return None
        requests_list = data.get("requests", [])
        list_info = data.get("list_info", {})

        if not requests_list:
            break

        all_requests.extend(requests_list)
        if not list_info.get("has_more_rows", False):
            break

        start_index += API_ROW_LIMIT

    if cutoff is None:   # no shift given: "updated" means updated since midnight
        cutoff = datetime.combine(date.today(), datetime.min.time())
    cutoff_ms = int(cutoff.timestamp() * 1000)
    pending_tickets = []
    updated_today_count = 0
    total_considered = 0

    for req in all_requests:
        req_id = req.get("id")

        severity = ticket_severity(req)

        # Exclude tickets with severity PM from counts and table.
        if is_pm_ticket(req):
            continue

        total_considered += 1
        last_ms = get_last_worklog_timestamp_ms(req_id, auth_token, base_url)
        last_dt = datetime.fromtimestamp(last_ms / 1000) if last_ms else None
        technician = req.get("technician") or {}

        status_data = req.get("status") or {}
        if isinstance(status_data, dict):
            ticket_status = status_data.get("name") or status_data.get("value") or status_data.get("display_value") or "In Progress"
        elif isinstance(status_data, str):
            ticket_status = status_data
        else:
            ticket_status = "In Progress"

        if last_ms and last_ms >= cutoff_ms:
            updated_today_count += 1
            continue

        pending_tickets.append({
            "ticket_id": f"#{req_id}",
            "title": req.get("subject", "No Subject"),
            "assigned_l1": technician.get("name", "Unassigned"),
            "severity": severity if severity else "N/A",
            "status": ticket_status,
            "last_worklog": str(last_dt.date()) if last_dt else "No record",
            "last_worklog_time": last_dt.strftime("%H:%M") if last_dt else "",
        })

    return {
        "tickets": pending_tickets,
        "pending_count": len(pending_tickets),
        "updated_today_count": updated_today_count,   # updated since `cutoff`
        "total_in_progress": total_considered,
        "cutoff": cutoff,
    }


def get_inprogress_tickets(cutoff: Optional[datetime] = None) -> Optional[List[Dict]]:
    """Fetch pending 'In Progress' tickets (no worklog since `cutoff`)."""
    summary = get_inprogress_summary(cutoff)
    if summary is None:
        return None
    return summary.get("tickets", [])


def get_last_worklog_date(request_id: int, auth_token: str, base_url: str) -> Optional[date]:
    """Get latest worklog date for a specific ticket."""
    latest_timestamp_ms = get_last_worklog_timestamp_ms(request_id, auth_token, base_url)
    if latest_timestamp_ms:
        return datetime.fromtimestamp(latest_timestamp_ms / 1000).date()
    return None


def get_request_worklogs(request_id: int, auth_token: str, base_url: str) -> Optional[List[Dict]]:
    """Fetch all worklogs for a request across API pages."""
    url = f"{base_url}/api/v3/requests/{request_id}/worklogs"
    headers = {"authtoken": auth_token}

    try:
        worklogs = []
        start_index = 1

        while True:
            input_data = {
                "list_info": {
                    "row_count": WORKLOG_ROW_LIMIT,
                    "start_index": start_index,
                }
            }
            response = requests.get(
                url,
                headers=headers,
                params={"input_data": json.dumps(input_data)},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                return None

            data = response.json()
            page_worklogs = data.get("worklogs", [])
            if not isinstance(page_worklogs, list) or not page_worklogs:
                break

            worklogs.extend(page_worklogs)
            list_info = data.get("list_info", {})
            if not isinstance(list_info, dict) or not list_info.get("has_more_rows", False):
                break

            start_index += WORKLOG_ROW_LIMIT

        if not worklogs:
            return None

        return worklogs
    except requests.exceptions.Timeout:
        print(f"[WARN] Worklog API timeout for #{request_id}")
    except requests.exceptions.RequestException as e:
        print(f"[WARN] Worklog API error for #{request_id}: {e}")
    except Exception as e:
        print(f"[WARN] Worklog check error for #{request_id}: {e}")

    return None


def get_worklog_timestamp_value(obj: Dict, field: str) -> int:
    """Extract ManageEngine timestamp milliseconds from a raw worklog field."""
    try:
        raw = obj.get(field, {})
        val = raw.get("value", 0) if isinstance(raw, dict) else raw
        return int(val) if val else 0
    except (ValueError, TypeError, AttributeError):
        return 0


def get_worklog_timestamp(w: Dict) -> int:
    return max(
        get_worklog_timestamp_value(w, "updated_time"),
        get_worklog_timestamp_value(w, "created_time"),
        get_worklog_timestamp_value(w, "start_time"),
        get_worklog_timestamp_value(w, "end_time"),
    )


def get_last_worklog_timestamp_ms(request_id: int, auth_token: str, base_url: str) -> int:
    worklogs = get_request_worklogs(request_id, auth_token, base_url)
    if not worklogs:
        return 0
    latest_worklog = max(worklogs, key=get_worklog_timestamp)
    return get_worklog_timestamp(latest_worklog)


def debug_worklog_ticket(request_id: int) -> bool:
    """Print raw timestamp summary for one request's worklogs."""
    base_url = CONFIG["mesdp"]["base_url"]
    auth_token = CONFIG["mesdp"]["auth_token"]
    worklogs = get_request_worklogs(request_id, auth_token, base_url)
    if worklogs is None:
        print(f"[ERROR] Failed to fetch worklogs for #{request_id}")
        return False

    print(f"\nWorklog debug for #{request_id}")
    print(f"Total worklogs returned by API: {len(worklogs)}")
    if not worklogs:
        return True

    sorted_worklogs = sorted(worklogs, key=get_worklog_timestamp, reverse=True)
    for idx, worklog in enumerate(sorted_worklogs[:5], start=1):
        timestamp_ms = get_worklog_timestamp(worklog)
        timestamp_text = (
            datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
            if timestamp_ms
            else "No timestamp"
        )
        owner = worklog.get("owner") or worklog.get("technician") or worklog.get("created_by") or {}
        owner_name = owner.get("name", "N/A") if isinstance(owner, dict) else str(owner)
        print(f"{idx}. {timestamp_text} | owner: {owner_name}")
    return True


# ─────────────────────────────────────────
# BUILD MESSAGES
# ─────────────────────────────────────────

# ── Email design ──────────────────────────────────────────────────────────────
# Corporate report style: white page, navy as the single brand colour, red and
# amber only for items that need attention. Table layout + inline styles +
# solid colours so it renders the same in Outlook desktop, Outlook web and Gmail.
_C = {
    "page": "#f2f4f7", "card": "#ffffff", "ink": "#1d2939", "body": "#344054", "muted": "#667085",
    "line": "#d0d5dd", "hair": "#eaecf0", "zebra": "#f9fafb", "head": "#f2f4f7",
    "navy": "#0b2a4a", "navy_soft": "#e9eef5",
    "red": "#b42318", "amber": "#b54708", "green": "#027a48",
}
_STALE_DAYS = 3
_FONT = "'Segoe UI', Arial, Helvetica, sans-serif"
_COMPANY = "CLL Systems"


def _worklog_age(last_worklog: str, today: date) -> Optional[int]:
    """Days since the last worklog, or None when there is no record."""
    try:
        return (today - datetime.strptime(str(last_worklog), "%Y-%m-%d").date()).days
    except ValueError:
        return None


def _age_text(age: Optional[int], last_worklog: str, last_time: str = "") -> Tuple[str, str, str]:
    """(colour, main label, sub label) for the Last worklog column."""
    if age is None:
        return _C["red"], "No worklog", "Never updated"
    if age <= 0:   # updated today, but before this shift's cutoff
        return _C["body"], "Before shift", escape(f"Today {last_time}".strip())
    label = "Yesterday" if age == 1 else f"{age} days ago"
    colour = _C["amber"] if age >= _STALE_DAYS else _C["body"]
    return colour, label, escape(str(last_worklog))


def _ticket_url(ticket_id: str) -> Optional[str]:
    rid = str(ticket_id).lstrip("#")
    base = str(CONFIG.get("mesdp", {}).get("base_url", "")).rstrip("/")
    if rid.isdigit() and base.startswith("https://"):
        return f"{base}/WorkOrder.do?woMode=viewWO&woID={rid}"
    return None


def _split_title(title: str) -> Tuple[str, str, str]:
    """'Genting | Commvault | Media Refresh' -> ('Genting', 'Commvault', 'Media Refresh')."""
    text = str(title or "")
    parts = [p.strip() for p in text.split("|") if p.strip()]
    if len(parts) == 1 and text.count(" I ") >= 2:   # "CLL I Generali I Cohesity"
        parts = [p.strip() for p in text.split(" I ") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], " | ".join(parts[2:])
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", text


def _section_heading(text: str, note: str = "") -> str:
    note_html = (f'<td align="right" valign="bottom" style="font-size:12px; color:{_C["muted"]}; white-space:nowrap;">'
                 f'{note}</td>') if note else ""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-bottom:2px solid {_C["navy"]}; margin-bottom:12px;"><tr>'
            f'<td valign="bottom" style="padding-bottom:6px; font-size:13px; font-weight:700; color:{_C["navy"]}; '
            f'text-transform:uppercase; letter-spacing:.8px;">{escape(text)}</td>{note_html}</tr></table>')


# ── Shift context (roster + SDP activity) ────────────────────────────────────

_ROSTER_UNAVAILABLE = object()


def _shift_window(shift_name: str, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Start and end of the current occurrence of `shift_name`."""
    now = now or datetime.now()
    start_hhmm, end_hhmm = SHIFT_TIMES[shift_name]
    sh, sm = _parse_hhmm(start_hhmm) or (SHIFT_HOURS[shift_name][0], 0)
    eh, em = _parse_hhmm(end_hhmm) or (SHIFT_HOURS[shift_name][1], 0)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if (sh, sm) > (eh, em) and (now.hour, now.minute) < (eh, em):
        start -= timedelta(days=1)          # night shift that started yesterday
    end = start.replace(hour=eh, minute=em)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _next_shift(shift_name: str, now: Optional[datetime] = None) -> Tuple[str, datetime]:
    """The shift after the current occurrence of `shift_name`, and its start."""
    _, end = _shift_window(shift_name, now)
    order = list(SHIFT_ORDER)
    return order[(order.index(shift_name) + 1) % len(order)], end


def worklog_cutoff(shift_name: str, now: Optional[datetime] = None) -> datetime:
    """Earliest worklog time that counts for this shift: the previous shift's
    final reminder (e.g. 07:30 for Morning, 19:30 for Night). Updates made
    after the previous shift was last reminded belong to this shift."""
    start, _ = _shift_window(shift_name, now)
    order = list(SHIFT_ORDER)
    prev = order[(order.index(shift_name) - 1) % len(order)]
    offsets = SHIFT_REMINDER_OFFSETS.get(prev) or DEFAULT_REMINDER_OFFSETS[prev]
    return start - timedelta(minutes=min(offsets))


def get_shift_roster(shift_name: str):
    """Current and next shift from the roster.

    Returns {"current": <roster result or None>, "next": <roster result or None>},
    None when no roster file is configured, or _ROSTER_UNAVAILABLE when one is
    configured but could not be read.
    """
    import roster
    if not roster.roster_path():
        return None
    start, _ = _shift_window(shift_name)
    next_name, next_start = _next_shift(shift_name)
    results = roster.get_on_shifts([(shift_name, start.date()), (next_name, next_start.date())])
    if not results or all(r is None for r in results):
        return _ROSTER_UNAVAILABLE
    for label, res in (("On shift", results[0]), ("Next shift", results[1])):
        if res:
            names = ", ".join(p["name"] + (" (first contact)" if p["first_contact"] else "") for p in res["people"])
            print(f"[roster] {label} ({res['shift']}, {res['date']:%d %b}): {names or 'nobody rostered'}")
    return {"current": results[0], "next": results[1]}


STATUS_IN_PROGRESS = "In Progress"
STATUS_ON_HOLD = "Onhold"       # the status name used in this SDP instance


_COUNT_MAX_PAGES = 20   # 2,000 tickets per count is far above any real queue


def _count_requests(criteria: List[Dict]) -> Optional[int]:
    """Number of non-PM requests matching an SDP search_criteria list.

    The tickets are listed and filtered here (rather than using the API's
    total_count) so PM tickets are excluded with the same rule as the
    worklog check."""
    base_url = CONFIG["mesdp"]["base_url"]
    count, start = 0, 1
    try:
        for _ in range(_COUNT_MAX_PAGES):
            list_info = {"row_count": API_ROW_LIMIT, "start_index": start, "search_criteria": criteria}
            r = requests.get(f"{base_url}/api/v3/requests",
                             headers={"authtoken": CONFIG["mesdp"]["auth_token"]},
                             params={"input_data": json.dumps({"list_info": list_info})},
                             timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                return None
            data = r.json()
            page = data.get("requests", [])
            count += sum(1 for req in page if isinstance(req, dict) and not is_pm_ticket(req))
            if not (data.get("list_info") or {}).get("has_more_rows") or not page:
                return count
            start += API_ROW_LIMIT
        return None   # unexpectedly large result: show "-" rather than a wrong number
    except (requests.exceptions.RequestException, ValueError, TypeError):
        return None


def get_shift_activity(shift_name: str) -> Optional[Dict]:
    """Ticket counts for the email (None if every API call fails):
    opened / closed since the shift started, plus the current In Progress and
    On Hold queue sizes."""
    start, _ = _shift_window(shift_name)
    now = datetime.now()
    window = [str(int(start.timestamp() * 1000)), str(int(now.timestamp() * 1000))]
    counts = {
        "opened": _count_requests([{"field": "created_time", "condition": "between", "values": window}]),
        "closed": _count_requests([{"field": "completed_time", "condition": "between", "values": window}]),
        "in_progress": _count_requests([{"field": "status.name", "condition": "is", "value": STATUS_IN_PROGRESS}]),
        "on_hold": _count_requests([{"field": "status.name", "condition": "is", "value": STATUS_ON_HOLD}]),
    }
    if all(v is None for v in counts.values()):
        return None
    print(f"[counts] Since {start:%H:%M}: {counts['opened']} opened, {counts['closed']} closed | "
          f"queue: {counts['in_progress']} in progress, {counts['on_hold']} on hold")
    return {**counts, "since": start, "until": now}


# ── Email blocks ─────────────────────────────────────────────────────────────

def _roster_rows(people: List[Dict], muted: bool = False) -> str:
    tiers: Dict[str, List[Dict]] = {}
    for p in people:
        tiers.setdefault(p.get("tier") or "Team", []).append(p)
    text_colour = _C["body"] if muted else _C["ink"]
    rows = ""
    for i, (tier, members) in enumerate(tiers.items()):
        names = ", ".join(
            escape(p["name"]) + (f' <span style="color:{_C["amber"]}; font-weight:700;">(First contact)</span>'
                                 if p.get("first_contact") else "")
            for p in members)
        top = f"border-top:1px solid {_C['hair']};" if i else ""
        rows += (f'<tr><td valign="top" style="padding:7px 12px 7px 0; width:64px; font-size:12px; font-weight:700; '
                 f'color:{_C["muted"]}; {top}">{escape(tier)}</td>'
                 f'<td valign="top" style="padding:7px 0; font-size:14px; color:{text_colour}; {top}">{names}</td></tr>')
    if not rows:
        rows = (f'<tr><td style="padding:7px 0; font-size:13px; color:{_C["muted"]};">'
                f'No engineers are rostered for this shift.</td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'


def _roster_subheading(kind: str, res: Optional[Dict], fallback_shift: str) -> str:
    shift = (res or {}).get("shift") or fallback_shift
    times = SHIFT_TIMES.get(shift)
    when = f"{times[0]}&ndash;{times[1]}" if times else ""
    day = f" &middot; {res['date']:%a %d %b}" if res and res.get("date") else ""
    return (f'<div style="margin:4px 0 2px; font-size:12px; color:{_C["muted"]};">'
            f'<strong style="color:{_C["navy"]}; text-transform:uppercase; letter-spacing:.5px;">{kind}</strong>'
            f' &nbsp;{escape(shift.capitalize())} &middot; {when}{day}</div>')


def _on_shift_block(on_shift, shift_name: Optional[str] = None) -> str:
    if on_shift is None:
        return ""
    if on_shift is _ROSTER_UNAVAILABLE or not isinstance(on_shift, dict):
        return (f'<tr><td class="px" style="padding:24px 32px 0; font-size:12px; color:{_C["muted"]};">'
                f'Duty roster unavailable (roster file could not be read).</td></tr>')

    current, nxt = on_shift.get("current"), on_shift.get("next")
    cur_name = (current or {}).get("shift") or shift_name or ""
    next_name = (nxt or {}).get("shift") or (_next_shift(shift_name)[0] if shift_name in SHIFT_TIMES else "")

    def unavailable() -> str:
        return (f'<div style="padding:7px 0; font-size:13px; color:{_C["muted"]};">'
                f'Not found in the roster for this date.</div>')

    # Current shift
    body = _roster_subheading("Current shift", current, cur_name)
    body += _roster_rows(current["people"]) if current else unavailable()
    first = [p for p in (current or {}).get("people", []) if p.get("first_contact")]
    if first:
        who = ", ".join(escape(p["name"]) + (f' ({escape(p["tier"])})' if p.get("tier") else "") for p in first)
        body += (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;">'
                 f'<tr><td style="border-left:3px solid {_C["navy"]}; background:{_C["navy_soft"]}; padding:9px 12px; '
                 f'font-size:13px; color:{_C["ink"]};"><strong>First point of contact:</strong> {who}</td></tr></table>')

    # Next shift (handover target)
    if next_name:
        body += (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">'
                 f'<tr><td style="border-top:1px dashed {_C["line"]}; padding-top:12px;">'
                 f'{_roster_subheading("Next shift", nxt, next_name)}'
                 f'{_roster_rows(nxt["people"], muted=True) if nxt else unavailable()}'
                 f'</td></tr></table>')

    return (f'<tr><td class="px" style="padding:28px 32px 0;">'
            f'{_section_heading("Duty roster")}{body}</td></tr>')


def _kpi(value, label: str, colour: str, first: bool = False) -> str:
    shown = "&ndash;" if value is None else str(value)
    border = "" if first else f"border-left:1px solid {_C['hair']};"
    return (f'<td class="kpi" align="center" valign="top" style="padding:14px 6px; {border}">'
            f'<div class="kpinum" style="font-size:26px; line-height:30px; font-weight:700; color:{colour};">{shown}</div>'
            f'<div class="kpilbl" style="font-size:11px; line-height:15px; color:{_C["muted"]}; margin-top:4px;">{label}</div></td>')


def _summary_block(pending: int, no_record: int, stale: int, activity: Optional[Dict]) -> str:
    def group(title: str, cells: str, width: str) -> str:
        return (f'<td class="stack grp" valign="top" width="{width}" style="padding:0;">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {_C["line"]};">'
                f'<tr><td style="background:{_C["head"]}; padding:7px 8px; font-size:11px; font-weight:700; color:{_C["muted"]}; '
                f'text-transform:uppercase; letter-spacing:.3px; white-space:nowrap; border-bottom:1px solid {_C["line"]};">{title}</td></tr>'
                f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'
                f'</td></tr></table></td>')

    gap = '<td class="stack gap" width="10" style="font-size:0; line-height:0;">&nbsp;</td>'
    worklog = group("Worklog compliance",
                    _kpi(pending, "Pending", _C["navy"], first=True)
                    + _kpi(no_record, "No worklog", _C["red"] if no_record else _C["navy"])
                    + _kpi(stale, f"{_STALE_DAYS}+ days old", _C["amber"] if stale else _C["navy"]),
                    "40%" if activity else "100%")
    groups = worklog
    note = ""
    if activity:
        in_progress = group("In progress", _kpi(activity.get("in_progress"), "Tickets", _C["navy"], first=True), "17%")
        on_hold = group("On hold", _kpi(activity.get("on_hold"), "Tickets", _C["amber"], first=True), "14%")
        shift = group(f"This shift &middot; {activity['since']:%H:%M}+",
                      _kpi(activity.get("opened"), "Opened", _C["navy"], first=True)
                      + _kpi(activity.get("closed"), "Closed", _C["green"]),
                      "27%")
        groups = in_progress + gap + on_hold + gap + worklog + gap + shift
        note = f"As at {activity['until']:%H:%M} &middot; PM tickets excluded"
    return (f'<tr><td class="px" style="padding:28px 32px 0;">'
            f'{_section_heading("Summary", note)}'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{groups}</tr></table></td></tr>')


def _ticket_row(t: Dict, zebra: bool) -> str:
    """One ticket. Wide screens get separate columns (class "dcol"); on phones
    those columns are hidden and the "mob" lines inside the subject cell show
    customer / owner instead."""
    colour, label, sub = _age_text(t["_age"], t.get("last_worklog", ""), t.get("last_worklog_time", ""))
    customer, product, rest = _split_title(t.get("title", ""))
    url = _ticket_url(t.get("ticket_id", ""))
    tid = escape(str(t.get("ticket_id", "")))
    tid_html = (f'<a href="{escape(url, quote=True)}" style="color:{_C["navy"]}; font-weight:700; text-decoration:underline;">{tid}</a>'
                if url else f'<strong style="color:{_C["navy"]};">{tid}</strong>')

    owner = escape(str(t.get("assigned_l1") or "Unassigned"))
    sev = str(t.get("severity", "") or "")
    sev_txt = escape(sev) if sev and sev.upper() not in {"N/A", "REQUEST"} else ""
    status = str(t.get("status", "") or "")
    status_txt = escape(status) if status and status.upper() != "IN PROGRESS" else ""
    sev_cell = " &middot; ".join(x for x in (sev_txt, status_txt) if x) or f'<span style="color:{_C["muted"]};">&ndash;</span>'

    context = " &middot; ".join(escape(x) for x in (customer, product) if x)
    mob_owner = owner + "".join(f" &middot; {x}" for x in (sev_txt, status_txt) if x)
    hidden = "display:none; mso-hide:all; max-height:0; overflow:hidden;"

    bg = _C["zebra"] if zebra else _C["card"]
    cell = f"border-top:1px solid {_C['hair']}; background:{bg}; font-size:13px; line-height:19px;"
    return (
        f'<tr>'
        f'<td class="idcol" valign="top" style="{cell} padding:10px 10px 10px 12px; white-space:nowrap;">{tid_html}</td>'
        f'<td class="dcol" valign="top" style="{cell} padding:10px; font-weight:700; color:{_C["ink"]};">{escape(customer) or "&ndash;"}</td>'
        f'<td class="dcol" valign="top" style="{cell} padding:10px; color:{_C["body"]};">{escape(product) or "&ndash;"}</td>'
        f'<td valign="top" style="{cell} padding:10px; color:{_C["ink"]};">'
        + (f'<div class="mob" style="{hidden} font-size:11px; font-weight:700; color:{_C["muted"]}; '
           f'text-transform:uppercase; letter-spacing:.4px;">{context}</div>' if context else "")
        + f'{escape(rest)}'
        f'<div class="mob" style="{hidden} font-size:12px; color:{_C["muted"]}; margin-top:3px;">{mob_owner}</div></td>'
        f'<td class="dcol" valign="top" style="{cell} padding:10px; color:{_C["body"]}; white-space:nowrap;">{owner}</td>'
        f'<td class="dcol" valign="top" style="{cell} padding:10px; color:{_C["body"]}; white-space:nowrap;">{sev_cell}</td>'
        f'<td class="agecol" valign="top" align="right" style="{cell} padding:10px 12px 10px 6px; text-align:right; white-space:nowrap;">'
        f'<div style="font-weight:700; color:{colour};">{label}</div>'
        f'<div class="agesub" style="font-size:11px; color:{_C["muted"]}; margin-top:2px;">{sub}</div></td>'
        f'</tr>'
    )


def build_email(shift_info: Dict, tickets: List[Dict], on_shift=None, activity: Optional[Dict] = None,
                shift_name: Optional[str] = None, final: bool = False) -> Tuple[str, str]:
    """Build the reminder email.

    on_shift:  result of get_shift_roster() (None hides the duty roster).
    activity:  result of get_shift_activity() (None hides opened/closed).
    """
    now = datetime.now()
    today = now.date()
    shift_label = shift_info["label"]
    shift_short = shift_label.split("(")[0].strip()
    hours_txt = shift_label[shift_label.find("(") + 1:shift_label.rfind(")")] if "(" in shift_label else ""

    enriched = [{**t, "_age": _worklog_age(t.get("last_worklog", ""), today)} for t in tickets]
    no_record = sum(1 for t in enriched if t["_age"] is None)
    stale = sum(1 for t in enriched if t["_age"] is not None and t["_age"] >= _STALE_DAYS)

    ends_txt = ""
    if shift_name in SHIFT_TIMES:
        _, end = _shift_window(shift_name, now)
        mins = int((end - now).total_seconds() // 60)
        if 0 < mins <= 12 * 60:
            ends_txt = (f"Shift ends in {mins} minutes" if mins < 60
                        else f"Shift ends in {mins // 60} h {mins % 60:02d} min")

    header = (
        f'<tr><td height="6" bgcolor="{_C["navy"]}" style="background:{_C["navy"]}; font-size:0; line-height:0;">&nbsp;</td></tr>'
        f'<tr><td class="px" style="padding:22px 32px 20px; border-bottom:1px solid {_C["line"]};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td class="stack" valign="top">'
        f'<div style="font-size:12px; font-weight:700; color:{_C["navy"]}; letter-spacing:1.5px; text-transform:uppercase;">'
        f'{_COMPANY} &nbsp;|&nbsp; Service Desk Operations</div>'
        f'<div style="font-size:22px; line-height:28px; font-weight:700; color:{_C["ink"]}; margin-top:8px;">'
        f'{"Final Worklog Reminder" if final else "Worklog Update Reminder"}</div>'
        f'<div style="font-size:14px; color:{_C["body"]}; margin-top:2px;">{escape(shift_short)}'
        f'{f" &nbsp;&middot;&nbsp; {escape(hours_txt)}" if hours_txt else ""}</div></td>'
        f'<td class="stack metacell" valign="top" align="right" style="font-size:12px; line-height:18px; color:{_C["muted"]}; '
        f'white-space:nowrap; padding-left:16px;">{now:%d %B %Y}<br>Generated {now:%H:%M}'
        f'{f"<br><strong style=\"color:{_C["amber"]};\">{ends_txt}</strong>" if ends_txt else ""}</td>'
        f'</tr></table></td></tr>'
    )

    since_txt = (f" (since {worklog_cutoff(shift_name, now):%H:%M})"
                 if shift_name in SHIFT_TIMES else "")
    intro = (
        f'<tr><td class="px" style="padding:24px 32px 0; font-size:14px; line-height:22px; color:{_C["body"]};">'
        + (f"Dear Team,<br>Please ensure every ticket listed below has a worklog for this shift{since_txt} "
           "(progress, current status and next action) <strong>before the end of the shift</strong>."
           if enriched else
           f"Dear Team,<br>All in-progress tickets have a worklog for this shift{since_txt}. "
           "Thank you for maintaining a complete handover.")
        + '</td></tr>'
    )

    if enriched:
        ordered = sorted(enriched, key=lambda t: (t["_age"] is not None, -(t["_age"] or 0)))
        rows = "".join(_ticket_row(t, i % 2 == 1) for i, t in enumerate(ordered))
        th = (f'background:{_C["head"]}; padding:8px 10px; font-size:11px; font-weight:700; color:{_C["muted"]}; '
              f'text-transform:uppercase; letter-spacing:.5px; border-bottom:1px solid {_C["line"]};')
        tickets_block = (
            f'<tr><td class="px" style="padding:28px 32px 0;">'
            f'{_section_heading("Tickets pending worklog", f"{len(enriched)} ticket{"s" if len(enriched) != 1 else ""} &middot; oldest first")}'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {_C["line"]};">'
            f'<tr><td class="idcol" style="{th} padding-left:12px;">Ticket</td>'
            f'<td class="dcol" style="{th}">Customer</td><td class="dcol" style="{th}">Product</td>'
            f'<td style="{th}">Subject</td><td class="dcol" style="{th}">Owner</td><td class="dcol" style="{th}">Severity</td>'
            f'<td class="agecol" style="{th} text-align:right; padding-right:12px;">Last worklog</td></tr>'
            f'{rows}</table></td></tr>'
        )
    else:
        tickets_block = ""

    footer = (
        f'<tr><td class="px" style="padding:32px 32px 28px;">'
        f'<div style="border-top:1px solid {_C["line"]}; padding-top:14px; font-size:11px; line-height:17px; color:{_C["muted"]};">'
        f'This is an automated notification generated from ManageEngine ServiceDesk Plus. Please do not reply to this email.<br>'
        f'{_COMPANY} &middot; Internal use only.</div></td></tr>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>Worklog Update Reminder</title>
<style>
  @media only screen and (max-width: 620px) {{
    .wrap {{ width: 100% !important; }}
    .px {{ padding-left: 16px !important; padding-right: 16px !important; }}
    .stack {{ display: block !important; width: 100% !important; text-align: left !important; padding-left: 0 !important; }}
    .metacell {{ padding-top: 12px !important; }}
    .kpi {{ padding: 10px 2px !important; }}
    .gap {{ height: 10px !important; }}
    .kpinum {{ font-size: 20px !important; line-height: 24px !important; }}
    .kpilbl {{ font-size: 10px !important; line-height: 13px !important; }}
    .idcol {{ width: 52px !important; padding-left: 8px !important; }}
    .agecol {{ width: 80px !important; }}
    .agesub {{ display: none !important; }}
    .dcol {{ display: none !important; }}
    .mob {{ display: block !important; max-height: none !important; overflow: visible !important; }}
  }}
</style>
</head>
<body style="margin:0; padding:0; background:{_C['page']}; font-family:{_FONT}; color:{_C['ink']};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{_C['page']}" style="background:{_C['page']};">
<tr><td align="center" style="padding:24px 10px;">
  <table role="presentation" class="wrap" width="100%" cellpadding="0" cellspacing="0" bgcolor="{_C['card']}"
         style="width:100%; max-width:1400px; background:{_C['card']}; border:1px solid {_C['line']}; font-family:{_FONT};">
    {header}
    {intro}
    {_on_shift_block(on_shift, shift_name)}
    {_summary_block(len(enriched), no_record, stale, activity)}
    {tickets_block}
    {footer}
  </table>
</td></tr>
</table>
</body>
</html>"""

    count_part = f"{len(tickets)} ticket{'s' if len(tickets) != 1 else ''} pending" if tickets else "all updated"
    title = "FINAL Worklog Reminder" if final else "Worklog Update Reminder"
    subject = f"[MESDP] {title} — {shift_short} | {count_part} | {now:%d %b %Y}"
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
        # Sanitise subject to prevent header injection
        safe_subject = subject.replace("\r", "").replace("\n", " ")

        # Validate recipients — skip any that are blank or malformed
        valid_recipients = []
        for addr in CONFIG["email"]["recipients"]:
            name, email_addr = parseaddr(str(addr))
            if "@" in email_addr:
                valid_recipients.append(email_addr)
            else:
                print(f"[WARN] Skipping invalid recipient: {addr!r}")
        if not valid_recipients:
            print("[ERROR] No valid recipients - aborting email send.")
            return False

        msg            = MIMEMultipart("alternative")
        msg["Subject"] = safe_subject
        msg["From"]    = CONFIG["email"]["sender"]
        msg["To"]      = ", ".join(valid_recipients)
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(CONFIG["email"]["smtp_server"], CONFIG["email"]["smtp_port"], timeout=REQUEST_TIMEOUT) as server:
            if CONFIG["email"].get("use_starttls", True):
                server.starttls(context=ssl.create_default_context())
            server.login(CONFIG["email"]["sender"], CONFIG["email"]["password"])
            server.sendmail(CONFIG["email"]["sender"], valid_recipients, msg.as_string())

        print("[OK] Email sent successfully.")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"[ERROR] SMTP Auth Failed: Verify credentials/app password or SMTP AUTH policy")
        print(f"   Error: {e}")
        return False
    except smtplib.SMTPException as e:
        print(f"[ERROR] SMTP Error: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Email Error: {e}")
        return False


def send_teams(payload: Dict) -> bool:
    """Send Teams message dengan error handling."""
    try:
        resp = requests.post(
            CONFIG["teams"]["webhook_url"],
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        if resp.status_code == 200:
            print("[OK] Teams message sent.")
            return True
        else:
            print(f"[ERROR] Teams error {resp.status_code}")
            return False
    except Exception as e:
        print(f"[ERROR] Teams error: {e}")
        return False


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def main():
    """Main workflow."""
    # Validate config
    if not validate_config():
        print("\n[ERROR] Config validation failed. Exiting.\n")
        sys.exit(1)

    # Parse arguments
    parser = argparse.ArgumentParser(description="MESDP Worklog Reminder")
    parser.add_argument("--shift", choices=list(SHIFT_ORDER),
                        help="Shift to send reminder (morning/night)")
    parser.add_argument("--schedule", action="store_true", 
                        help="Run scheduler mode (continuous, auto-send at reminder times)")
    parser.add_argument("--final", action="store_true",
                        help="Send/preview the final (last) reminder of the shift")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build output without sending email/Teams")
    parser.add_argument("--sandbox", action="store_true",
                        help="Isolated mode: skip API and sending, use local sample tickets")
    parser.add_argument("--debug-ticket", type=int,
                        help="Fetch and print latest worklog timestamps for one ticket, then exit")
    parser.add_argument("--preview-file", default="preview_worklog_email.html",
                        help="Path to save generated HTML preview (default: preview_worklog_email.html)")
    args = parser.parse_args()

    if args.debug_ticket:
        sys.exit(0 if debug_worklog_ticket(args.debug_ticket) else 1)

    # Guard: preview-file must stay within the project directory
    _base_dir = os.path.abspath(os.path.dirname(__file__))
    _preview_path = os.path.abspath(os.path.join(_base_dir, args.preview_file))
    if not _preview_path.startswith(_base_dir + os.sep) and _preview_path != _base_dir:
        print(f"[ERROR] Invalid --preview-file path: must be inside the project directory.")
        sys.exit(1)
    args.preview_file = _preview_path

    # Scheduler mode
    if args.schedule:
        schedule_reminders()
        return

    # Single run mode
    auto_shift = get_auto_shift() if not args.shift else args.shift
    shift_info = SHIFTS[auto_shift]

    print(f"\n{'='*55}")
    print(f"  MESDP Worklog Reminder - {shift_info['label']}")
    print(f"  Run time: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")

    # Pull tickets
    if args.sandbox:
        print("[sandbox] Sandbox mode enabled: using local sample tickets (no API call).")
        tickets = get_sample_tickets()
    else:
        print("Connecting to MESDP API...")
        cutoff = worklog_cutoff(auto_shift)
        print(f"   Worklog counted from {cutoff:%d %b %H:%M}")
        tickets = get_inprogress_tickets(cutoff)

    if tickets is None:
        print("[ERROR] Failed to pull tickets. Exiting.")
        if args.dry_run:
            subject, html = build_api_alert_email(shift_info, args.final)
            save_preview_html(args.preview_file, html)
            print(f"[skip] Dry-run: API alert not sent ({subject})")
        else:
            send_api_alert(shift_info, args.final)
        sys.exit(1)

    print(f"[WARN] {len(tickets)} ticket(s) have no worklog for this shift yet.")

    # Build & send
    activity = None if args.sandbox else get_shift_activity(auto_shift)
    subject, html = build_email(shift_info, tickets, get_shift_roster(auto_shift), activity, auto_shift, final=args.final)
    preview_result = save_preview_html(args.preview_file, html)

    if args.sandbox:
        email_result = False
        teams_result = False
        email_status_text = "Skipped (sandbox)"
        print("[skip] Sandbox mode: email sending skipped.")
    elif args.dry_run:
        email_result = False
        teams_result = False
        email_status_text = "Skipped (dry-run)"
        print("[skip] Dry-run mode: email sending skipped.")
    else:
        email_result = send_email(subject, html)
        teams_result = False  # Teams disabled
        email_status_text = "✅ Yes" if email_result else "❌ No"

    # Status report
    print("\n" + "="*55)
    print("  EXECUTION SUMMARY")
    print(f"  Shift: {shift_info['label']}")
    print(f"  Tickets pending: {len(tickets)}")
    print(f"  Preview generated: {'Yes' if preview_result else 'No'}")
    print(f"  Email sent: {email_status_text}")
    print(f"  Teams sent: {'Yes' if teams_result else 'Disabled'}")
    print("="*55 + "\n")

    if args.sandbox or args.dry_run:
        result_code = 0 if preview_result else 1
    else:
        result_code = 0 if email_result else 1
    sys.exit(result_code)


if __name__ == "__main__":
    main()
