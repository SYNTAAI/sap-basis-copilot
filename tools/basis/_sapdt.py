"""Shared SAP date/time parsing for the Basis tools.

The JCo bridge returns DATE fields either as the internal 'YYYYMMDD' (when they
come from RFC_READ_TABLE, which hands back the raw field) or as ISO
'YYYY-MM-DD' (when JCo formats a structure field, e.g. ENQUEUE_READ.GTDATE).
Times arrive as 'HHMMSS' or 'HH:MM:SS'. Both shapes are handled here so no tool
has to guess.
"""

from datetime import datetime

_EPOCH_FLOOR = datetime(1990, 1, 1)


def parse_sap_datetime(date_str, time_str=""):
    """Return a datetime, or None when the value is empty/initial/unparseable."""
    d = (date_str or "").strip().replace("-", "")
    t = (time_str or "").strip().replace(":", "")
    if not d or d in ("00000000", "0"):
        return None
    t = (t or "000000")[:6].ljust(6, "0")
    try:
        dt = datetime.strptime(d[:8] + t, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return dt if dt > _EPOCH_FLOOR else None


def parse_sap_timestamp(ts):
    """Parse a packed 'YYYYMMDDHHMMSS[..]' char timestamp (TSP01.RQCRETIME)."""
    s = (ts or "").strip()
    if len(s) < 8 or not s[:8].isdigit():
        return None
    return parse_sap_datetime(s[:8], s[8:14])


def age_hours(dt, now=None):
    """Whole-ish hours between dt and now, or None."""
    if dt is None:
        return None
    now = now or datetime.utcnow()
    return round((now - dt).total_seconds() / 3600.0, 1)


def age_bucket(hours):
    """Bucket an age in hours into the SM12-style bands."""
    if hours is None:
        return "unknown"
    if hours < 1:
        return "<1h"
    if hours < 4:
        return "1-4h"
    if hours < 24:
        return "4-24h"
    return ">24h"


def clean_sap_str(value):
    """Strip the 0xFFFF filler SAP uses to pad lock arguments, plus whitespace."""
    if value is None:
        return ""
    return str(value).replace("￿", "").replace("\x00", "").strip()
