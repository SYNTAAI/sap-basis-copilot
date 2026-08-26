"""Spool tools – 1 tool registered via register_spool_tools().

Covers: SP01 / SP02 / SPAD (spool requests and output requests).

Reads TSP01 (spool requests) and TSP02 (output requests). Spool *content* is
never read -- only the request metadata an administrator needs to triage a
printing problem or a retention backlog.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

from ._sapdt import age_hours, clean_sap_str, parse_sap_timestamp

logger = logging.getLogger("syntaai.tools.basis.spool")

# TSP02.PJSTATUS – output request status.
_OUTPUT_STATUS_LABELS = {
    "<": "waiting to be sent",
    ">": "in transfer to host spooler",
    "+": "being generated",
    "-": "waiting for the spool work process",
    "P": "waiting on a print request",
    "S": "sent to host spooler",
    "C": "completed",
    "F": "completed (front-end)",
    "E": "error",
    "A": "aborted",
    "B": "problem, partially printed",
    "T": "time exceeded",
}
_FAILED_OUTPUT = {"E", "A", "B", "T"}
_WAITING_OUTPUT = {"<", ">", "+", "-", "P"}

_RETENTION_DAYS = 7
_MAX_DETAIL = 20


def register_spool_tools(mcp, connector):
    """Register spool tools on the given FastMCP instance."""

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

    # ------------------------------------------------------------------
    # 1. get_spool_status
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_spool_status(
        hours_back: int = 24,
        max_rows: int = 5000,
    ) -> dict:
        """Show spool and output request status (SP01 / SP02): failed prints, busy devices and retention backlog.

        Only request metadata is read -- never the spool content itself.

        Args:
            hours_back: Only requests created within this window (default 24 hours).
            max_rows: Cap on rows read from each of TSP01 and TSP02 (default 5000).
        """
        try:
            hours_back = min(max(int(hours_back), 1), 8760)
            max_rows = min(max(int(max_rows), 1), 20000)
            now = datetime.utcnow()
            cutoff_ts = (now - timedelta(hours=hours_back)).strftime("%Y%m%d%H%M%S") + "00"

            spool_rows, spool_failed = await _safe_table_read(
                "TSP01",
                ["RQIDENT", "RQCLIENT", "RQOWNER", "RQDEST", "RQCRETIME",
                 "RQFINAL", "RQDELETED", "RQDOCTYPE", "RQPAPER", "RQTITLE"],
                f"RQCRETIME >= '{cutoff_ts}'",
                max_rows,
            )
            if spool_failed:
                return {
                    "source": "live_sap",
                    "error": "Could not read TSP01 (spool requests).",
                    "tool": "get_spool_status",
                }

            by_owner: Counter = Counter()
            by_device: Counter = Counter()
            spool_requests = []
            for row in spool_rows:
                created = parse_sap_timestamp(row.get("RQCRETIME"))
                owner = clean_sap_str(row.get("RQOWNER")) or "(unknown)"
                device = clean_sap_str(row.get("RQDEST")) or "(none)"
                by_owner[owner] += 1
                by_device[device] += 1
                spool_requests.append({
                    "spool_id": clean_sap_str(row.get("RQIDENT")),
                    "owner": owner,
                    "device": device,
                    "client": clean_sap_str(row.get("RQCLIENT")),
                    "title": clean_sap_str(row.get("RQTITLE"))[:60],
                    "doc_type": clean_sap_str(row.get("RQDOCTYPE")),
                    "created_at": created.isoformat() + "Z" if created else None,
                    "age_hours": age_hours(created, now),
                    "deleted_flag": clean_sap_str(row.get("RQDELETED")),
                })

            # Retention hygiene: still present well past the usual 7-day life.
            retention_cutoff = _RETENTION_DAYS * 24
            aged = sorted(
                (s for s in spool_requests
                 if (s["age_hours"] or 0) > retention_cutoff and s["deleted_flag"] != "X"),
                key=lambda s: s["age_hours"] or 0, reverse=True,
            )

            out_rows, out_failed = await _safe_table_read(
                "TSP02",
                ["PJIDENT", "PJDEST", "PJCREATIME", "PJSTATUS", "PJOWNER",
                 "PJCLIENT", "PJSIZE", "PJINFOMSG", "PJREASON", "PJHOSTID"],
                f"PJCREATIME >= '{cutoff_ts}'",
                max_rows,
            )

            out_by_status: Counter = Counter()
            failed_outputs = []
            waiting_count = 0
            size_total = 0
            largest = []

            if not out_failed:
                for row in out_rows:
                    status = clean_sap_str(row.get("PJSTATUS")) or "?"
                    created = parse_sap_timestamp(row.get("PJCREATIME"))
                    out_by_status[status] += 1
                    try:
                        size = int(clean_sap_str(row.get("PJSIZE")) or 0)
                    except ValueError:
                        size = 0
                    size_total += size
                    entry = {
                        "spool_id": clean_sap_str(row.get("PJIDENT")),
                        "device": clean_sap_str(row.get("PJDEST")) or "(none)",
                        "owner": clean_sap_str(row.get("PJOWNER")),
                        "status": status,
                        "status_label": _OUTPUT_STATUS_LABELS.get(status, "unknown"),
                        "size_bytes": size,
                        "host": clean_sap_str(row.get("PJHOSTID")),
                        "created_at": created.isoformat() + "Z" if created else None,
                        "age_hours": age_hours(created, now),
                        "message": clean_sap_str(row.get("PJINFOMSG"))[:80],
                    }
                    largest.append(entry)
                    if status in _FAILED_OUTPUT:
                        failed_outputs.append(entry)
                    elif status in _WAITING_OUTPUT:
                        waiting_count += 1

                largest.sort(key=lambda e: e["size_bytes"], reverse=True)
                failed_outputs.sort(key=lambda e: e["age_hours"] or 0, reverse=True)

            if failed_outputs or len(aged) > 100:
                risk = "HIGH" if len(failed_outputs) >= 10 else "MEDIUM"
            else:
                risk = "LOW"

            return {
                "source": "live_sap",
                "hours_back": hours_back,
                "spool_requests": {
                    "total": len(spool_requests),
                    "top_owners": [{"owner": o, "requests": c} for o, c in by_owner.most_common(10)],
                    "top_devices": [{"device": d, "requests": c} for d, c in by_device.most_common(10)],
                    "older_than_7_days": len(aged),
                    "aged_sample": aged[:_MAX_DETAIL],
                },
                "output_requests": {
                    "available": not out_failed,
                    "total": len(out_rows) if not out_failed else 0,
                    "by_status": {s: {"count": c, "label": _OUTPUT_STATUS_LABELS.get(s, "unknown")}
                                  for s, c in out_by_status.most_common()},
                    "failed_count": len(failed_outputs),
                    "waiting_count": waiting_count,
                    "total_size_bytes": size_total,
                    "largest": largest[:10],
                    "failed_sample": failed_outputs[:_MAX_DETAIL],
                },
                "risk": risk,
                "note": "Spool content is never read; only request metadata. "
                        "Spool number-range usage (NRIV object SPO_NUM) is not included.",
                "checked_at": now.isoformat() + "Z",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_spool_status"}
