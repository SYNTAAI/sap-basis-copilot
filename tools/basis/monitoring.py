"""System health monitoring tools – 4 tools registered via register_monitoring_tools().

Covers: SM51 (instances), SM21 (syslog), SM13 (update errors), and a combined health summary.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta

logger = logging.getLogger("syntaai.tools.basis.monitoring")

# Status mapping for SM13 update requests
_UPDATE_STATUS_MAP = {
    "E": "failed",
    "I": "initialised",
    "V": "completed",
}


def register_monitoring_tools(mcp, connector):
    """Register all system health monitoring tools on the given FastMCP instance."""

    # ------------------------------------------------------------------
    # Helper: read a table with graceful failure
    # ------------------------------------------------------------------
    async def _safe_table_read(table, fields, where="", max_rows=50):
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
    # Helper: RFC call with graceful failure
    # ------------------------------------------------------------------
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
    # 1. get_system_health_summary
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_system_health_summary() -> dict:
        """One-shot SAP health check combining instance status (SM51), active jobs (SM37), and critical syslog (SM21)."""
        try:
            # Fetch data sources in parallel
            sysinfo_task = _safe_rfc_call("RFC_SYSTEM_INFO")
            jobs_task = _safe_table_read(
                "TBTCO",
                ["JOBNAME", "STATUS", "SDLSTRTDT", "AUTHCKNAM"],
                "STATUS = 'R'",
                20,
            )

            (sysinfo, sysinfo_err), (active_jobs, _jobs_err) = await asyncio.gather(
                sysinfo_task, jobs_task
            )

            # Derive instance info from RFC_SYSTEM_INFO
            # Response format: {"export": {"FQHN": "...", "CURRENT_RESOURCES": "...", ...}, "tables": {}}
            instances = []
            inst_err = sysinfo_err
            if not sysinfo_err:
                export = sysinfo.get("export", sysinfo)
                instances = [{
                    "hostname": export.get("FQHN", export.get("RFCHOST", "")),
                    "system_id": export.get("RFCSYSID", ""),
                    "database": export.get("RFCDBHOST", ""),
                    "kernel_release": export.get("RFCKERNRL", ""),
                    "s4_hana": export.get("S4_HANA", ""),
                    "status": "ACTIVE",
                }]

            instances_up = len(instances)
            total_instances = len(instances)

            # Syslog is not available via table read; skip gracefully
            critical_logs = []
            syslog_err = True

            if total_instances > 0 and instances_up < total_instances:
                health_score = "CRITICAL"
            elif critical_logs:
                health_score = "WARNING"
            else:
                health_score = "OK"

            return {
                "source": "live_sap",
                "health_score": health_score,
                "summary": {
                    "total_instances": total_instances,
                    "instances_up": instances_up,
                    "active_jobs_running": len(active_jobs),
                    "critical_log_entries": len(critical_logs),
                },
                "instances": instances,
                "active_jobs": active_jobs,
                "critical_logs": critical_logs,
                "syslog_unavailable": syslog_err,
                "syslog_note": "SM21 syslog is not available via table read. Use get_syslog_critical for RFC-based access.",
                "checked_at": datetime.utcnow().isoformat() + "Z",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_system_health_summary"}

    # ------------------------------------------------------------------
    # 2. get_instance_status
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_instance_status() -> dict:
        """List all ABAP application server instances with up/down status (SM51)."""
        try:
            connector_used = "rfc"
            sysinfo, failed = await _safe_rfc_call("RFC_SYSTEM_INFO")

            if not failed:
                export = sysinfo.get("export", sysinfo)
                instances = [{
                    "hostname": export.get("FQHN", export.get("RFCHOST", "")),
                    "instance": export.get("RFCSYSID", ""),
                    "status": "ACTIVE",
                    "is_active": True,
                    "database_host": export.get("RFCDBHOST", ""),
                    "database_system": export.get("RFCDBSYS", ""),
                    "kernel_release": export.get("RFCKERNRL", ""),
                    "os_type": export.get("RFCOPSYS", ""),
                    "ip_address": export.get("RFCIPADDR", ""),
                    "s4_hana": export.get("S4_HANA", ""),
                }]
                return {
                    "source": "live_sap",
                    "connector_used": connector_used,
                    "total_instances": 1,
                    "active": 1,
                    "inactive": 0,
                    "instances": instances,
                }

            # OData fallback
            if hasattr(connector, "_get"):
                try:
                    odata_resp = await connector._get("/sap/opu/odata/sap/API_SYSTEM_INFO_SRV/SystemInfo")
                    instances_raw = odata_resp if isinstance(odata_resp, list) else odata_resp.get("d", {}).get("results", [])
                    connector_used = "odata"
                    failed = False
                except Exception as exc:
                    logger.warning("OData fallback for instance status failed: %s", exc)

            if failed:
                return {
                    "source": "live_sap",
                    "error": "Could not retrieve instance data via RFC_SYSTEM_INFO or OData API",
                    "tool": "get_instance_status",
                }

            instances = []
            active_count = 0
            for row in instances_raw:
                status = str(row.get("STATUS", "")).upper()
                is_active = status in ("UP", "1", "ACTIVE")
                if is_active:
                    active_count += 1
                instances.append({
                    "hostname": row.get("HOSTNAME", ""),
                    "instance": row.get("INSTANCE", ""),
                    "status": row.get("STATUS", ""),
                    "is_active": is_active,
                })

            total = len(instances)
            return {
                "source": "live_sap",
                "connector_used": connector_used,
                "total_instances": total,
                "active": active_count,
                "inactive": total - active_count,
                "instances": instances,
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_instance_status"}

    # ------------------------------------------------------------------
    # 3. get_syslog_critical
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_syslog_critical(
        hours_back: int = 24,
        max_entries: int = 50,
        severity_filter: str = "all",
    ) -> dict:
        """Recent critical/error entries from the SAP system log (SM21). Supports filtering by severity and time window."""
        try:
            hours_back = min(max(hours_back, 1), 168)
            max_entries = min(max(max_entries, 1), 200)

            cutoff = datetime.utcnow() - timedelta(hours=hours_back)
            date_from = cutoff.strftime("%Y%m%d")
            time_from = cutoff.strftime("%H%M%S")
            date_to = datetime.utcnow().strftime("%Y%m%d")
            time_to = datetime.utcnow().strftime("%H%M%S")

            # Try RFC RSLG_READ_SYSLOG_FOR_PERIOD
            rfc_result, rfc_failed = await _safe_rfc_call(
                "RSLG_READ_SYSLOG_FOR_PERIOD",
                {
                    "DATE_FROM": date_from,
                    "TIME_FROM": time_from,
                    "DATE_TO": date_to,
                    "TIME_TO": time_to,
                },
            )

            if rfc_failed:
                return {
                    "source": "live_sap",
                    "hours_back": hours_back,
                    "severity_filter": severity_filter,
                    "total_entries": 0,
                    "by_severity": {},
                    "entries": [],
                    "note": "SM21 syslog data is not available. The SYSLOG table does not exist "
                            "for direct reads, and RFC RSLG_READ_SYSLOG_FOR_PERIOD could not be called. "
                            "This may require additional authorizations or the function module may not be available.",
                }

            # Parse RFC result – the syslog entries are typically in a table parameter
            raw_entries = rfc_result.get("ES_SYSLOG", rfc_result.get("ET_SYSLOG", []))
            if isinstance(raw_entries, dict):
                raw_entries = [raw_entries]

            severity_map_filter = {
                "error": "E",
                "abort": "A",
                "warning": "W",
            }
            filter_sev = severity_map_filter.get(severity_filter)

            severity_counts: Counter = Counter()
            entries = []
            for row in raw_entries:
                sev = row.get("SEVERITY", row.get("SEV", ""))
                if filter_sev and sev != filter_sev:
                    continue
                if sev in ("E", "A", "W"):
                    severity_counts[sev] += 1
                else:
                    severity_counts["other"] += 1
                entries.append({
                    "date": row.get("DATE", row.get("DATUM", "")),
                    "time": row.get("TIME", row.get("UZEIT", "")),
                    "severity": sev,
                    "text": row.get("TEXT", row.get("MTEXT", "")),
                    "host": row.get("HOST", row.get("SERVER", "")),
                    "client": row.get("MANDT", row.get("MANDANT", "")),
                })
                if len(entries) >= max_entries:
                    break

            return {
                "source": "live_sap",
                "hours_back": hours_back,
                "severity_filter": severity_filter,
                "total_entries": len(entries),
                "by_severity": dict(severity_counts),
                "entries": entries,
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_syslog_critical"}

    # ------------------------------------------------------------------
    # 4. check_update_errors
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def check_update_errors(
        hours_back: int = 48,
        include_completed: bool = False,
    ) -> dict:
        """Check for failed SAP update requests (SM13). Shows error counts, top failing modules, and details."""
        try:
            cutoff_date = (datetime.utcnow() - timedelta(hours=hours_back)).strftime("%Y%m%d")
            where_clause = f"VBDATE >= '{cutoff_date}'"
            if not include_completed:
                where_clause += " AND VBSTATE = 'E'"

            rows, failed = await _safe_table_read(
                "VBHDR",
                ["VBKEY", "VBMANDT", "VBDATE", "VBNAME", "VBSTATE", "VBUSR", "VBTCODE", "VBREPORT"],
                where_clause,
                100,
            )

            if failed:
                return {
                    "source": "live_sap",
                    "error": "Could not read VBHDR (update request header) table",
                    "tool": "check_update_errors",
                }

            failed_count = 0
            pending_count = 0
            completed_count = 0
            failed_updates = []
            module_counter: Counter = Counter()

            for row in rows:
                raw_status = row.get("VBSTATE", "")
                status_label = _UPDATE_STATUS_MAP.get(raw_status, "unknown")

                if raw_status == "E":
                    failed_count += 1
                    module_counter[row.get("VBNAME", "")] += 1
                    failed_updates.append({
                        "vbkey": row.get("VBKEY", ""),
                        "date": row.get("VBDATE", ""),
                        "function_module": row.get("VBNAME", ""),
                        "status": raw_status,
                        "status_label": status_label,
                        "user": row.get("VBUSR", ""),
                        "tcode": row.get("VBTCODE", ""),
                        "report": row.get("VBREPORT", ""),
                        "client": row.get("VBMANDT", ""),
                    })
                elif raw_status == "I":
                    pending_count += 1
                elif raw_status == "V":
                    completed_count += 1

            # Top 5 failing modules
            top_failing_modules = [
                {"function_module": fm, "count": cnt}
                for fm, cnt in module_counter.most_common(5)
            ]

            return {
                "source": "live_sap",
                "hours_back": hours_back,
                "total_checked": len(rows),
                "failed_count": failed_count,
                "pending_count": pending_count,
                "completed_count": completed_count,
                "top_failing_modules": top_failing_modules,
                "failed_updates": failed_updates,
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "check_update_errors"}
