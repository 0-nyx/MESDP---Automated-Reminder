"""
Read who is on shift from the CST-NOC shift roster workbook.

Expected layout (e.g. sheet "NOV-DEC" of NOC-SHIFT-ROSTERS-FY26.xlsx):
  column B          DATE (one row per day, data starts below the header)
  header row - 1    tier per column group: "L1", "L1.5", "L2" (merged cells)
  header row        engineer names, from column E onwards
  data cells        "Morning", "Night", "Off", "REST", "AL", ...
  red font          that engineer is the first contact for the shift

A date can appear on more than one sheet (an old month sheet that runs into
the next month); the sheet whose first date is the latest wins. Hidden sheets
are ignored.

The workbook is copied to a temp file before reading so an open Excel window
does not block it. Any problem returns None: the reminder must still go out.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from config import CONFIG

FIRST_NAME_COL = 5          # column E
MAX_SCAN_COLS = 40
MAX_HEADER_ROWS = 12
TIER_LABELS = {"L1", "L1.5", "L2", "L3"}


def roster_path() -> str:
    return str((CONFIG.get("roster", {}) or {}).get("path", "") or "").strip()


def shift_date_for(shift_start_hour: int, shift_end_hour: int, now: Optional[datetime] = None) -> date:
    """Roster date a shift belongs to. A shift that crosses midnight (night)
    is listed on the day it started, so before its end time it is yesterday's."""
    now = now or datetime.now()
    if shift_start_hour > shift_end_hour and now.hour < shift_end_hour:
        return now.date() - timedelta(days=1)
    return now.date()


def _is_red(cell) -> bool:
    try:
        rgb = cell.font.color.rgb
    except AttributeError:
        return False
    return isinstance(rgb, str) and rgb.upper().endswith("FF0000")


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _sheet_layout(ws) -> Optional[Tuple[int, Dict[int, str], Dict[int, str]]]:
    """Return (first_data_row, {col: name}, {col: tier}) or None if not a roster sheet."""
    first_data_row = None
    for r in range(1, MAX_HEADER_ROWS + 1):
        if _as_date(ws.cell(r, 2).value):
            first_data_row = r
            break
    if not first_data_row or first_data_row < 2:
        return None

    name_row = first_data_row - 1
    names: Dict[int, str] = {}
    empty_run = 0
    for c in range(FIRST_NAME_COL, FIRST_NAME_COL + MAX_SCAN_COLS):
        v = ws.cell(name_row, c).value
        if isinstance(v, str) and v.strip():
            names[c] = v.strip()
            empty_run = 0
        else:
            empty_run += 1
            if empty_run >= 2 and names:
                break
    if not names:
        return None

    tiers: Dict[int, str] = {}
    current = ""
    for c in range(FIRST_NAME_COL, max(names) + 1):
        v = ws.cell(name_row - 1, c).value
        if isinstance(v, str) and v.strip().upper() in TIER_LABELS:
            current = v.strip().upper()
        tiers[c] = current
    return first_data_row, names, tiers


def _find_day(wb, day: date):
    """Pick (ws, row, names, tiers) for `day` from the newest visible sheet."""
    best = None
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        layout = _sheet_layout(ws)
        if not layout:
            continue
        first_row, names, tiers = layout
        first_date = _as_date(ws.cell(first_row, 2).value)
        for r in range(first_row, ws.max_row + 1):
            if _as_date(ws.cell(r, 2).value) == day:
                if best is None or first_date > best[0]:
                    best = (first_date, ws, r, names, tiers)
                break
    return best[1:] if best else None


def _people_on(wb, shift_name: str, day: date, path: str) -> Optional[Dict]:
    found = _find_day(wb, day)
    if not found:
        print(f"[roster] no row for {day:%d %b %Y} in {os.path.basename(path)}")
        return None
    ws, row, names, tiers = found
    wanted = shift_name.strip().lower()
    people: List[Dict] = []
    for col, name in names.items():
        cell = ws.cell(row, col)
        if isinstance(cell.value, str) and cell.value.strip().lower() == wanted:
            people.append({"name": name, "tier": tiers.get(col, ""), "first_contact": _is_red(cell)})
    return {"shift": shift_name, "date": day, "sheet": ws.title, "people": people}


def get_on_shifts(queries: List[Tuple[str, date]], path: Optional[str] = None) -> Optional[List[Optional[Dict]]]:
    """Look up several (shift_name, roster date) pairs with one read of the
    workbook. Returns one result per query (None for a date not in the
    roster), or None when the workbook itself is unavailable.

    Each result: {"shift", "date", "sheet", "people": [{"name", "tier", "first_contact"}]}
    with people in roster column order.
    """
    path = path or roster_path()
    if not path or not os.path.exists(path):
        return None
    try:
        import openpyxl
    except ImportError:
        print("[roster] skipped: Python package 'openpyxl' is not installed.")
        return None

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        shutil.copyfile(path, tmp)
        wb = openpyxl.load_workbook(tmp, data_only=True)
        return [_people_on(wb, shift, day, path) for shift, day in queries]
    except Exception as e:  # never block the reminder on the roster
        print(f"[roster] could not be read: {e}")
        return None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def get_on_shift(shift_name: str, day: date, path: Optional[str] = None) -> Optional[Dict]:
    """Who works `shift_name` ("morning"/"night") on roster date `day` (None if unavailable)."""
    results = get_on_shifts([(shift_name, day)], path)
    return results[0] if results else None
