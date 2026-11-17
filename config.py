"""Runtime configuration loader for MESDP Worklog Reminder.

This module contains NO secrets. Real values (MESDP token, SMTP password,
recipients) are stored per user in:

    %APPDATA%\\CLL MESDP\\app_config.json

The file is written by the Scheduler Control GUI (first-run setup).
Set the MESDP_CONFIG_FILE environment variable to use a different file
(useful for testing).
"""

from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
from typing import Any, Dict, Optional, Tuple


def _default_config_path() -> str:
    override = os.environ.get("MESDP_CONFIG_FILE", "").strip()
    if override:
        return os.path.abspath(override)
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "CLL MESDP", "app_config.json")


USER_CONFIG_FILE = _default_config_path()

DEFAULT_CONFIG: Dict[str, Any] = {
    "mesdp": {
        "base_url": "",
        "auth_token": "",
    },
    "email": {
        "smtp_server": "",
        "smtp_port": 587,
        "use_starttls": True,
        "sender": "",
        "password": "",
        "recipients": [],
    },
    "teams": {
        "webhook_url": "",
    },
    # CST-NOC shift roster workbook used to list who is on shift in the email.
    "roster": {
        "path": "",
    },
}

# Shared mutable dict: other modules hold a reference to this object, so it
# is always updated in place (never rebound).
CONFIG: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)


def _merge_defaults(data: Any) -> Dict[str, Any]:
    """Return a full config dict: defaults overlaid with known sections from data."""
    merged = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(data, dict):
        return merged
    for section, defaults in merged.items():
        incoming = data.get(section)
        if isinstance(incoming, dict):
            for key in defaults:
                if key in incoming:
                    defaults[key] = incoming[key]
    recipients = merged["email"].get("recipients")
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.replace(";", ",").split(",")]
    if not isinstance(recipients, list):
        recipients = []
    merged["email"]["recipients"] = [str(r).strip() for r in recipients if str(r).strip()]
    try:
        merged["email"]["smtp_port"] = int(merged["email"].get("smtp_port") or 587)
    except (TypeError, ValueError):
        merged["email"]["smtp_port"] = 587
    merged["email"]["use_starttls"] = bool(merged["email"].get("use_starttls", True))
    merged["mesdp"]["base_url"] = str(merged["mesdp"].get("base_url", "")).strip().rstrip("/")
    return merged


# ── Secrets at rest (Windows DPAPI) ───────────────────────────────────────────
# auth_token and password are stored as "dpapi:<base64>", encrypted for the
# current Windows user. Only that user on that machine can decrypt them.

SECRET_FIELDS = (("mesdp", "auth_token"), ("email", "password"))
_DPAPI_PREFIX = "dpapi:"


def _dpapi(data: bytes, encrypt: bool) -> Optional[bytes]:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect_secret(value: str) -> str:
    value = str(value or "")
    if not value or value.startswith(_DPAPI_PREFIX):
        return value
    enc = _dpapi(value.encode("utf-8"), encrypt=True)
    return _DPAPI_PREFIX + base64.b64encode(enc).decode("ascii") if enc else value


def unprotect_secret(value: str) -> Optional[str]:
    """Plaintext for a stored value; None if it cannot be decrypted here."""
    value = str(value or "")
    if not value.startswith(_DPAPI_PREFIX):
        return value
    try:
        dec = _dpapi(base64.b64decode(value[len(_DPAPI_PREFIX):]), encrypt=False)
    except (ValueError, TypeError):
        return None
    return dec.decode("utf-8") if dec is not None else None


def _map_secrets(cfg: Dict[str, Any], fn) -> Tuple[Dict[str, Any], bool]:
    """Apply fn to each secret field. Returns (copy, all_ok)."""
    out, ok = copy.deepcopy(cfg), True
    for section, key in SECRET_FIELDS:
        sec = out.get(section)
        if isinstance(sec, dict) and sec.get(key):
            val = fn(sec[key])
            if val is None:
                ok, val = False, ""
            sec[key] = val
    return out, ok


def _is_stored_plaintext(data: Dict[str, Any]) -> bool:
    return any(
        isinstance(data.get(s), dict) and data[s].get(k) and not str(data[s][k]).startswith(_DPAPI_PREFIX)
        for s, k in SECRET_FIELDS
    )


def _read_json(path: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return False, f"File not found: {path}", None
    except (OSError, json.JSONDecodeError) as e:
        return False, str(e), None
    if not isinstance(data, dict):
        return False, "Config file must contain a JSON object.", None
    return True, "", data


def _apply(new_config: Dict[str, Any]) -> None:
    CONFIG.clear()
    CONFIG.update(new_config)


def reload_config_from_disk() -> Dict[str, Any]:
    """Reload CONFIG from USER_CONFIG_FILE (in place). Missing file -> defaults.

    Secrets are decrypted in memory. A file still holding plaintext secrets
    (older version) is rewritten encrypted.
    """
    ok, _err, data = _read_json(USER_CONFIG_FILE)
    merged = _merge_defaults(data if ok else None)
    plain, _ = _map_secrets(merged, unprotect_secret)
    _apply(plain)
    if ok and data is not None and _is_stored_plaintext(data):
        try:
            _atomic_write_json(USER_CONFIG_FILE, _map_secrets(merged, protect_secret)[0])
        except OSError:
            pass
    return CONFIG


def runtime_config_ready(config: Optional[Dict[str, Any]] = None) -> bool:
    """True when every value needed to pull tickets and send email is present."""
    cfg = CONFIG if config is None else config
    if not isinstance(cfg, dict):
        return False
    mesdp = cfg.get("mesdp") or {}
    email = cfg.get("email") or {}
    required = [
        mesdp.get("base_url"),
        mesdp.get("auth_token"),
        email.get("smtp_server"),
        email.get("smtp_port"),
        email.get("sender"),
        email.get("password"),
    ]
    if not all(str(v or "").strip() for v in required):
        return False
    return bool(email.get("recipients"))


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".app_config.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def save_user_config(payload: Dict[str, Any]) -> bool:
    """Merge payload over current CONFIG, write to disk, update CONFIG in place."""
    if not isinstance(payload, dict):
        return False
    combined = copy.deepcopy(CONFIG)
    for section, values in payload.items():
        if isinstance(values, dict) and isinstance(combined.get(section), dict):
            combined[section].update(values)
    merged = _merge_defaults(combined)
    try:
        _atomic_write_json(USER_CONFIG_FILE, _map_secrets(merged, protect_secret)[0])
    except OSError:
        return False
    _apply(merged)
    return True


def export_config_backup(path: str) -> bool:
    """Copy the saved config to path. Secrets stay DPAPI-encrypted, so the
    backup can only be restored by the same Windows user. False if nothing saved."""
    ok, _err, data = _read_json(USER_CONFIG_FILE)
    if not ok or data is None:
        return False
    try:
        _atomic_write_json(os.path.abspath(path), _map_secrets(_merge_defaults(data), protect_secret)[0])
    except OSError:
        return False
    return True


def import_config_backup(path: str) -> Tuple[bool, str, Dict[str, Any]]:
    """Read a backup file. Returns (ok, error_message, config_dict with plaintext secrets)."""
    ok, err, data = _read_json(path)
    if not ok or data is None:
        return False, err, {}
    if not any(isinstance(data.get(s), dict) for s in ("mesdp", "email")):
        return False, "Backup does not contain 'mesdp' or 'email' settings.", {}
    plain, secrets_ok = _map_secrets(_merge_defaults(data), unprotect_secret)
    if not secrets_ok:
        return False, ("This backup was created by a different Windows user or PC, so its "
                       "token/password cannot be decrypted here. Re-enter them manually."), {}
    return True, "", plain


reload_config_from_disk()
