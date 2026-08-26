"""Lock entry tools – 1 tool registered via register_lock_tools().

Covers: SM12 (lock entries) via the remote-enabled function module ENQUEUE_READ.
"""

import logging
from collections import Counter
from datetime import datetime

from ._sapdt import age_bucket, age_hours, clean_sap_str, parse_sap_datetime

logger = logging.getLogger("syntaai.tools.basis.locks")

# ENQUEUE_READ GMODE values. 'E' and 'X' block other writers; 'S' does not.
_LOCK_MODE_LABELS = {
    "E": "exclusive (cumulative)",
    "X": "exclusive (not cumulative)",
    "S": "shared / read",
    "O": "optimistic",
}

_MAX_DETAIL = 20


def register_lock_tools(mcp, connector):
    """Register lock entry tools on the given FastMCP instance."""

    async def _safe_rfc_call(function, parameters=None):
        """Call an RFC function, returning (result, error_flag). Never raises."""
        if not hasattr(connector, "_rfc_call"):
            return {}, True
        try:
            result = await connector._rfc_call(function, parameters)
            return result, False
        except Exception as exc:
            logger.warning("RFC %s call failed: %s", function, exc)
            return {}, True

    # ------------------------------------------------------------------
    # 1. get_lock_entries
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_lock_entries(
        stale_hours: int = 4,
        max_rows: int = 5000,
        client: str = "",
    ) -> dict:
        """Show current SAP lock entries (SM12): who holds which lock, for how long, and which look stale.

        A lock that has been held for hours is almost always a hung dialog session or a
        cancelled background job that never released it -- not something to delete
        blindly. Deleting a live lock can corrupt the document the holder is editing.
        Identify the holder and the work process first, and confirm the session is
        genuinely dead before anyone touches SM12.

        Args:
            stale_hours: Age above which a lock is flagged for review (default 4).
            max_rows: Cap on lock entries read from the system (default 5000).
            client: SAP client to read; defaults to the connection's client.
        """
        try:
            stale_hours = max(int(stale_hours), 1)
            max_rows = min(max(int(max_rows), 1), 20000)
            gclient = (client or getattr(connector, "connection", {}).get("client", "") or "").strip()

            result, failed = await _safe_rfc_call(
                "ENQUEUE_READ",
                {"GCLIENT": gclient, "GNAME": "", "GARG": "", "GUNAME": "",
                 "LOCAL": "", "FAST": ""},
            )
            if failed:
                return {
                    "source": "live_sap",
                    "error": "ENQUEUE_READ could not be called. This function module requires "
                             "authorization object S_ENQUE (activity 03). Check the RFC user's roles.",
                    "tool": "get_lock_entries",
                }

            rows = result.get("tables", {}).get("ENQ", [])[:max_rows]
            now = datetime.utcnow()

            by_user: Counter = Counter()
            by_object: Counter = Counter()
            by_mode: Counter = Counter()
            buckets: Counter = Counter()
            enriched = []

            for row in rows:
                owner = clean_sap_str(row.get("GUNAME"))
                obj = clean_sap_str(row.get("GNAME"))
                mode = clean_sap_str(row.get("GMODE")) or "?"
                held_at = parse_sap_datetime(row.get("GTDATE"), row.get("GTTIME"))
                hours = age_hours(held_at, now)

                by_user[owner or "(unknown)"] += 1
                by_object[obj or "(unknown)"] += 1
                by_mode[mode] += 1
                buckets[age_bucket(hours)] += 1

                enriched.append({
                    "user": owner,
                    "lock_object": obj,
                    "lock_argument": clean_sap_str(row.get("GARG"))[:80],
                    "mode": mode,
                    "mode_label": _LOCK_MODE_LABELS.get(mode, "unknown"),
                    "tcode": clean_sap_str(row.get("GTCODE")),
                    "host": clean_sap_str(row.get("GTHOST")),
                    "work_process": clean_sap_str(row.get("GTWP")),
                    "client": clean_sap_str(row.get("GCLIENT")),
                    "held_since": held_at.isoformat() + "Z" if held_at else None,
                    "age_hours": hours,
                })

            stale = sorted(
                (e for e in enriched if (e["age_hours"] or 0) >= stale_hours),
                key=lambda e: e["age_hours"] or 0,
                reverse=True,
            )
            # Users holding an unusual number of locks are the other thing worth seeing.
            heavy_holders = [
                {"user": u, "locks_held": c}
                for u, c in by_user.most_common(10) if c >= 5
            ]

            if stale or heavy_holders:
                risk = "HIGH" if len(stale) >= 5 else "MEDIUM"
            else:
                risk = "LOW"

            return {
                "source": "live_sap",
                "client": gclient,
                "stale_hours": stale_hours,
                "total_locks": len(enriched),
                "by_mode": {m: {"count": c, "label": _LOCK_MODE_LABELS.get(m, "unknown")}
                            for m, c in by_mode.most_common()},
                "by_age": dict(buckets),
                "top_users": [{"user": u, "locks_held": c} for u, c in by_user.most_common(10)],
                "top_objects": [{"lock_object": o, "locks": c} for o, c in by_object.most_common(10)],
                "stale_lock_count": len(stale),
                "stale_locks": stale[:_MAX_DETAIL],
                "heavy_holders": heavy_holders,
                "risk": risk,
                "guidance": "A long-held lock is usually a hung dialog or a cancelled batch job. "
                            "Trace the user, host and work process before considering SM12 deletion.",
                "checked_at": now.isoformat() + "Z",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_lock_entries"}
