"""SAP date parsing and formatting helpers."""

from datetime import datetime, timedelta
from typing import Optional


def parse_sap_date(sap_date_str) -> Optional[datetime]:
    """Parse SAP OData date formats into a datetime.

    Handles:
      - /Date(1697932800000)/   (OData Edm.DateTime)
      - "2024-01-15"            (ISO string)
      - "20240115"              (SAP internal YYYYMMDD)
      - ""  / None              → None
    """
    if not sap_date_str:
        return None

    raw = str(sap_date_str).strip()
    if not raw or raw == "00000000":
        return None

    # OData /Date(...)/ format
    if "/Date(" in raw:
        try:
            ts = int(raw.split("(")[1].split(")")[0]) / 1000
            return datetime.fromtimestamp(ts)
        except (ValueError, IndexError):
            return None

    # ISO  YYYY-MM-DD
    if len(raw) == 10 and raw[4] == "-":
        try:
            return datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None

    # SAP internal YYYYMMDD
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            return None

    return None


def days_since(sap_date_str) -> Optional[int]:
    """Return the number of days between *sap_date_str* and today, or None."""
    dt = parse_sap_date(sap_date_str)
    if dt is None:
        return None
    return (datetime.now() - dt).days


def days_until(sap_date_str) -> Optional[int]:
    """Return the number of days from today until *sap_date_str*, or None."""
    dt = parse_sap_date(sap_date_str)
    if dt is None:
        return None
    return (dt - datetime.now()).days


def fmt_date(sap_date_str) -> str:
    """Format a SAP date as 'YYYY-MM-DD' or 'Never'."""
    dt = parse_sap_date(sap_date_str)
    if dt is None:
        return "Never"
    return dt.strftime("%Y-%m-%d")
