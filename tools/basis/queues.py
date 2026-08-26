"""RFC queue tools – 2 tools registered via register_queue_tools().

Covers: SM58 (transactional RFC) and SMQ1 / SMQ2 (queued RFC, outbound and inbound).

Both read tables directly. The queue-overview function modules named in the SMQ1
API (TRFC_QOUT_GET_QUEUES / TRFC_QIN_GET_QUEUES) do not exist on this system --
TFDIR has no entry for either -- so TRFCQOUT and TRFCQIN are read instead, which
is the same data SMQ1/SMQ2 display.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

from ._sapdt import age_hours, clean_sap_str, parse_sap_datetime

logger = logging.getLogger("syntaai.tools.basis.queues")

# ARFCSSTATE.ARFCSTATE / TRFCQOUT.QSTATE values worth naming.
_TRFC_STATE_LABELS = {
    "RECORDED": "recorded, not yet sent",
    "SENDED": "sent, awaiting confirmation",
    "EXECUTED": "executed successfully",
    "CPICERR": "communication error",
    "SYSFAIL": "remote system/application failure",
    "MAILED": "error notification mailed",
    "READ": "read by the receiver",
}
_QRFC_STATE_LABELS = {
    "READY": "ready to run",
    "RUNNING": "executing",
    "STOP": "held (queue locked, usually deliberate)",
    "NOSEND": "flagged not to send",
    "SYSFAIL": "remote system/application failure",
    "CPICERR": "communication error",
    "WAITING": "waiting on a predecessor",
    "RETRY": "scheduled for retry",
}
_FAILED_STATES = {"CPICERR", "SYSFAIL"}
_HELD_STATES = {"STOP", "NOSEND"}

_MAX_DETAIL = 20


def register_queue_tools(mcp, connector):
    """Register RFC queue tools on the given FastMCP instance."""

    async def _safe_table_read(table, fields, where="", max_rows=5000):
        """Read a table, returning (rows, error_flag). Never raises."""
        if not hasattr(connector, "_table_read"):
            return [], True
        try:
            rows = await connector._table_read(table, fields=fields, where=where, max_rows=max_rows)
            return rows, False
        except Exception as exc:
            logger.warning("%s table read failed: %s", table, exc)
            return [], True

    def _window_date(hours_back):
        return (datetime.utcnow() - timedelta(hours=hours_back)).strftime("%Y%m%d")

    # ------------------------------------------------------------------
    # 1. get_trfc_queue_status
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_trfc_queue_status(
        hours_back: int = 24,
        max_rows: int = 5000,
    ) -> dict:
        """Show transactional RFC (tRFC) status (SM58): stuck and failed calls by destination and function module.

        Args:
            hours_back: Only entries recorded within this window (default 24 hours).
            max_rows: Cap on ARFCSSTATE rows read (default 5000).
        """
        try:
            hours_back = min(max(int(hours_back), 1), 720)
            max_rows = min(max(int(max_rows), 1), 20000)

            rows, failed = await _safe_table_read(
                "ARFCSSTATE",
                ["ARFCDEST", "ARFCSTATE", "ARFCFNAM", "ARFCUSER",
                 "ARFCDATUM", "ARFCUZEIT", "ARFCRETURN", "ARFCMSG",
                 "ARFCTCODE", "ARFCRETRYS"],
                f"ARFCDATUM >= '{_window_date(hours_back)}'",
                max_rows,
            )
            if failed:
                return {
                    "source": "live_sap",
                    "error": "Could not read ARFCSSTATE (tRFC status table).",
                    "tool": "get_trfc_queue_status",
                }

            now = datetime.utcnow()
            by_state: Counter = Counter()
            by_dest: Counter = Counter()
            dest_errors: Counter = Counter()
            by_fm: Counter = Counter()
            flagged = []
            oldest_unprocessed = None

            for row in rows:
                state = clean_sap_str(row.get("ARFCSTATE")).upper()
                dest = clean_sap_str(row.get("ARFCDEST")) or "(none)"
                fm = clean_sap_str(row.get("ARFCFNAM")) or "(unknown)"
                when = parse_sap_datetime(row.get("ARFCDATUM"), row.get("ARFCUZEIT"))
                hours = age_hours(when, now)

                by_state[state or "(blank)"] += 1
                by_dest[dest] += 1
                by_fm[fm] += 1
                if state in _FAILED_STATES:
                    dest_errors[dest] += 1

                if state != "EXECUTED" and hours is not None:
                    if oldest_unprocessed is None or hours > oldest_unprocessed["age_hours"]:
                        oldest_unprocessed = {
                            "destination": dest, "function_module": fm,
                            "state": state, "age_hours": hours,
                            "recorded_at": when.isoformat() + "Z",
                        }

                if state in _FAILED_STATES:
                    flagged.append({
                        "destination": dest,
                        "function_module": fm,
                        "state": state,
                        "state_label": _TRFC_STATE_LABELS.get(state, "unknown"),
                        "user": clean_sap_str(row.get("ARFCUSER")),
                        "tcode": clean_sap_str(row.get("ARFCTCODE")),
                        "retries": clean_sap_str(row.get("ARFCRETRYS")),
                        "recorded_at": when.isoformat() + "Z" if when else None,
                        "age_hours": hours,
                        "message": clean_sap_str(row.get("ARFCMSG")),
                    })

            flagged.sort(key=lambda e: e["age_hours"] or 0, reverse=True)
            error_total = sum(by_state.get(s, 0) for s in _FAILED_STATES)
            stuck_recorded = oldest_unprocessed is not None and \
                oldest_unprocessed["state"] == "RECORDED" and \
                (oldest_unprocessed["age_hours"] or 0) > 24

            if error_total > 50 or stuck_recorded:
                risk = "HIGH"
            elif error_total > 0:
                risk = "MEDIUM"
            else:
                risk = "LOW"

            return {
                "source": "live_sap",
                "hours_back": hours_back,
                "total_entries": len(rows),
                "by_state": {s: {"count": c, "label": _TRFC_STATE_LABELS.get(s, "unknown")}
                             for s, c in by_state.most_common()},
                "error_entries": error_total,
                "top_destinations": [
                    {"destination": d, "entries": c, "errors": dest_errors.get(d, 0),
                     "error_share_pct": round(100.0 * dest_errors.get(d, 0) / c, 1) if c else 0.0}
                    for d, c in by_dest.most_common(10)
                ],
                "top_function_modules": [
                    {"function_module": f, "entries": c} for f, c in by_fm.most_common(10)
                ],
                "oldest_unprocessed": oldest_unprocessed,
                "flagged_count": len(flagged),
                "flagged": flagged[:_MAX_DETAIL],
                "risk": risk,
                "checked_at": now.isoformat() + "Z",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_trfc_queue_status"}

    # ------------------------------------------------------------------
    # 2. get_qrfc_queue_status
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_qrfc_queue_status(
        direction: str = "both",
        hours_back: int = 24,
        max_rows: int = 5000,
    ) -> dict:
        """Show queued RFC (qRFC) status (SMQ1 outbound / SMQ2 inbound): queue depth, held queues and failures.

        STOP is normally deliberate -- somebody locked the queue in SMQ1 -- so it is
        reported as "held", not as a failure. SYSFAIL and CPICERR are the states that
        need attention.

        Args:
            direction: 'outbound' (SMQ1), 'inbound' (SMQ2) or 'both' (default).
            hours_back: Only entries recorded within this window (default 24 hours).
            max_rows: Cap on rows read per direction (default 5000).
        """
        try:
            direction = (direction or "both").lower().strip()
            if direction not in ("outbound", "inbound", "both"):
                return {"source": "live_sap",
                        "error": "direction must be 'outbound', 'inbound' or 'both'",
                        "tool": "get_qrfc_queue_status"}
            hours_back = min(max(int(hours_back), 1), 720)
            max_rows = min(max(int(max_rows), 1), 20000)
            cutoff = _window_date(hours_back)
            now = datetime.utcnow()

            targets = []
            if direction in ("outbound", "both"):
                targets.append(("outbound", "TRFCQOUT", "SMQ1"))
            if direction in ("inbound", "both"):
                targets.append(("inbound", "TRFCQIN", "SMQ2"))

            directions = {}
            any_failed = False
            total_flagged = 0

            for label, table, tcode in targets:
                rows, failed = await _safe_table_read(
                    table,
                    ["MANDT", "QNAME", "DEST", "QCOUNT", "QSTATE", "QRFCUSER",
                     "QRFCFNAM", "QRFCDATUM", "QRFCUZEIT", "NOSEND", "ERRMESS"],
                    f"QRFCDATUM >= '{cutoff}'",
                    max_rows,
                )
                if failed:
                    any_failed = True
                    directions[label] = {"tcode": tcode, "error": f"Could not read {table}"}
                    continue

                by_state: Counter = Counter()
                queue_depth: Counter = Counter()
                queue_meta = {}
                for row in rows:
                    qname = clean_sap_str(row.get("QNAME")) or "(unnamed)"
                    state = clean_sap_str(row.get("QSTATE")).upper() or "(blank)"
                    when = parse_sap_datetime(row.get("QRFCDATUM"), row.get("QRFCUZEIT"))
                    by_state[state] += 1
                    queue_depth[qname] += 1
                    meta = queue_meta.setdefault(qname, {
                        "queue": qname,
                        "destination": clean_sap_str(row.get("DEST")),
                        "states": Counter(),
                        "oldest": when,
                        "error_text": "",
                    })
                    meta["states"][state] += 1
                    if when and (meta["oldest"] is None or when < meta["oldest"]):
                        meta["oldest"] = when
                    if not meta["error_text"]:
                        meta["error_text"] = clean_sap_str(row.get("ERRMESS"))

                flagged = []
                for qname, meta in queue_meta.items():
                    states = set(meta["states"])
                    bad = states & _FAILED_STATES
                    held = states & _HELD_STATES
                    if not bad and not held:
                        continue
                    flagged.append({
                        "queue": qname,
                        "destination": meta["destination"],
                        "depth": queue_depth[qname],
                        "condition": "failed" if bad else "held",
                        "states": dict(meta["states"]),
                        "first_entry_age_hours": age_hours(meta["oldest"], now),
                        "error_text": meta["error_text"][:120],
                    })
                flagged.sort(key=lambda q: (q["condition"] != "failed", -(q["first_entry_age_hours"] or 0)))
                total_flagged += sum(1 for q in flagged if q["condition"] == "failed")

                directions[label] = {
                    "tcode": tcode,
                    "source_table": table,
                    "total_entries": len(rows),
                    "queue_count": len(queue_meta),
                    "by_state": {s: {"count": c, "label": _QRFC_STATE_LABELS.get(s, "unknown")}
                                 for s, c in by_state.most_common()},
                    "top_queues_by_depth": [
                        {"queue": q, "depth": d, "destination": queue_meta[q]["destination"]}
                        for q, d in queue_depth.most_common(10)
                    ],
                    "flagged_count": len(flagged),
                    "flagged_queues": flagged[:_MAX_DETAIL],
                }

            risk = "HIGH" if total_flagged >= 5 else ("MEDIUM" if total_flagged else "LOW")
            return {
                "source": "live_sap",
                "direction": direction,
                "hours_back": hours_back,
                "directions": directions,
                "failed_queue_count": total_flagged,
                "risk": risk,
                "note": "TRFC_QOUT_GET_QUEUES / TRFC_QIN_GET_QUEUES are not present on this "
                        "system (no TFDIR entry), so TRFCQOUT / TRFCQIN are read directly. "
                        "TRFC_QOUT_OVERVIEW and TRFC_QIN_OVERVIEW do exist and are "
                        "remote-enabled if a function-module source is preferred later.",
                "partial": any_failed,
                "checked_at": now.isoformat() + "Z",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_qrfc_queue_status"}
