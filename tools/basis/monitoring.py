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

    async def _syslog_severity_counts(hours_back=1):
        """Return (by_severity_dict, None) or ({}, reason) — never raises.

        Reads the R3Syslog message log through CCMS (GETTREE + GETMLHIS) and counts
        by severity. Used by the health summary so a syslog failure is reported as
        unavailable rather than silently counted as zero.
        """
        if not hasattr(connector, "_rfc_batch"):
            return {}, "batch RFC bridge unavailable"
        try:
            logon = {"function": "BAPI_XMI_LOGON",
                     "params": {"EXTCOMPANY": "SyntaAI", "EXTPRODUCT": "MCP",
                                "INTERFACE": "XAL", "VERSION": "1.0"}}
            tid_fields = ("MTSYSID", "MTMCNAME", "MTNUMRANGE", "MTUID",
                          "MTCLASS", "MTINDEX", "EXTINDEX")
            tree = await connector._rfc_batch([logon, {
                "function": "BAPI_SYSTEM_MON_GETTREE",
                "params": {"EXTERNAL_USER_NAME": "SYNTAAI_MCP",
                           "MONITOR_NAME": {"MS_NAME": "SAP CCMS Monitor Templates",
                                            "MONI_NAME": "Entire System"}}}])
            if not tree.get("success") or len(tree.get("results", [])) < 2:
                return {}, "CCMS tree unavailable: " + str(tree.get("error") or "no data")
            nodes = tree["results"][1].get("tables", {}).get("TREE_NODES", [])
            areas = [n for n in nodes
                     if str(n.get("OBJECTNAME", "")).strip().lower() == "r3syslog"
                     and str(n.get("MTCLASS", "")).strip() == "101"]
            if not areas:
                return {}, "no R3Syslog nodes in CCMS tree"
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            now = _dt.now(_tz.utc)
            start = now - _td(hours=max(int(hours_back), 1))
            fmt = lambda d: d.strftime("%Y%m%d%H%M%S")
            calls = [logon]
            for a in areas:
                calls.append({"function": "BAPI_SYSTEM_MTE_GETMLHIS",
                              "params": {"EXTERNAL_USER_NAME": "SYNTAAI_MCP",
                                         "TID": {f: a.get(f, "") for f in tid_fields},
                                         "START_TIMESTAMP": fmt(start),
                                         "END_TIMESTAMP": fmt(now)}})
            hist = await connector._rfc_batch(calls)
            if not hist.get("success"):
                return {}, "CCMS message-log read failed: " + str(hist.get("error"))
            from collections import Counter as _C
            sev = _C()
            for res in hist["results"][1:]:
                if not isinstance(res, dict):
                    continue
                for ln in res.get("tables", {}).get("MSG_LINE_DATA", []):
                    v = str(ln.get("VALUEFLTRD", ln.get("VALUEORIG", ""))).strip()
                    sev[{"1": "info", "2": "warning", "3": "error"}.get(v, "info")] += 1
            return dict(sev), None
        except Exception as e:
            return {}, str(e)

    # ------------------------------------------------------------------
    # 1. get_system_health_summary
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_system_health_summary() -> dict:
        """One-shot SAP health check combining instance status (SM51), active jobs (SM37), and critical syslog (SM21).

        Each component is reported independently. If a component's underlying call
        fails, that component is returned as {"status": "unavailable", "reason": ...}
        and the overall health is UNKNOWN -- a failed call is never counted as zero,
        because "no data" and "nothing wrong" are different answers.
        """
        components = {}
        any_unavailable = False

        # --- instances (RFC_SYSTEM_INFO) ---
        sysinfo, sysinfo_err = await _safe_rfc_call("RFC_SYSTEM_INFO")
        if sysinfo_err:
            components["instances"] = {"status": "unavailable",
                                       "reason": "RFC_SYSTEM_INFO call failed"}
            any_unavailable = True
        else:
            ex = sysinfo.get("export", sysinfo)
            components["instances"] = {
                "status": "available",
                "total": 1, "up": 1,
                "system_id": ex.get("RFCSYSID", ""),
                "host": ex.get("RFCHOST", ""),
                "kernel_release": ex.get("RFCKERNRL", ""),
            }

        # --- background jobs (TBTCO) ---
        job_rows, jobs_err = await _safe_table_read(
            "TBTCO", ["JOBNAME", "STATUS"], "", 500)
        if jobs_err:
            components["jobs"] = {"status": "unavailable",
                                  "reason": "TBTCO read failed"}
            any_unavailable = True
        else:
            from collections import Counter as _C
            labels = {"R": "active", "F": "finished", "A": "cancelled",
                      "S": "released", "Y": "ready", "P": "scheduled"}
            c = _C(labels.get(r.get("STATUS", ""), "other") for r in job_rows)
            components["jobs"] = {"status": "available",
                                  "active": c.get("active", 0),
                                  "cancelled": c.get("cancelled", 0),
                                  "by_status": dict(c)}

        # --- critical syslog (CCMS R3Syslog message log) ---
        sev, syslog_err = await _syslog_severity_counts(hours_back=1)
        if syslog_err:
            components["syslog"] = {"status": "unavailable", "reason": syslog_err}
            any_unavailable = True
        else:
            components["syslog"] = {"status": "available",
                                    "errors": sev.get("error", 0),
                                    "warnings": sev.get("warning", 0),
                                    "window_hours": 1}

        # --- overall health ---
        if any_unavailable:
            health = "UNKNOWN"
        else:
            inst = components["instances"]
            syslog_errors = components["syslog"].get("errors", 0)
            if inst.get("up", 0) < inst.get("total", 0):
                health = "CRITICAL"
            elif syslog_errors > 0 or components["jobs"].get("cancelled", 0) > 0:
                health = "WARNING"
            else:
                health = "OK"

        return {
            "source": "live_sap",
            "health": health,
            "components": components,
            "checked_at": datetime.utcnow().isoformat() + "Z",
        }

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
        external_user_name: str = "SYNTAAI_MCP",
    ) -> dict:
        """Recent critical/error entries from the SAP system log (SM21), read from CCMS.

        The classic RFC RSLG_READ_SYSLOG_FOR_PERIOD is not remote-enabled on modern
        systems, so this reads the R3Syslog message log through the CCMS monitoring
        architecture: BAPI_SYSTEM_MON_GETTREE locates the per-area syslog nodes and
        BAPI_SYSTEM_MTE_GETMLHIS returns their message history with timestamp,
        severity and rendered text. Requires a stateful XMI session (batch RFC).

        Args:
            hours_back: time window, 1..168 hours (default 24).
            max_entries: cap on returned detail entries, 1..200 (default 50).
            severity_filter: all | error | warning | info.
            external_user_name: name registered with the CCMS XAL interface.
        """
        try:
            hours_back = min(max(int(hours_back), 1), 168)
            max_entries = min(max(int(max_entries), 1), 200)
            if not hasattr(connector, "_rfc_batch"):
                return {"source": "live_sap", "error": "CCMS syslog needs the batch RFC bridge.",
                        "tool": "get_syslog_critical"}

            logon = {"function": "BAPI_XMI_LOGON",
                     "params": {"EXTCOMPANY": "SyntaAI", "EXTPRODUCT": "MCP",
                                "INTERFACE": "XAL", "VERSION": "1.0"}}
            tid_fields = ("MTSYSID", "MTMCNAME", "MTNUMRANGE", "MTUID",
                          "MTCLASS", "MTINDEX", "EXTINDEX")

            # 1. locate the R3Syslog message-log MTEs (MTCLASS 101) per area.
            tree = await connector._rfc_batch([logon, {
                "function": "BAPI_SYSTEM_MON_GETTREE",
                "params": {"EXTERNAL_USER_NAME": external_user_name,
                           "MONITOR_NAME": {"MS_NAME": "SAP CCMS Monitor Templates",
                                            "MONI_NAME": "Entire System"}}}])
            if not tree.get("success") or len(tree.get("results", [])) < 2:
                return {"source": "live_sap", "total_entries": 0, "by_severity": {}, "entries": [],
                        "note": "CCMS monitor tree unavailable: " + str(tree.get("error") or "no data"),
                        "hours_back": hours_back}
            nodes = tree["results"][1].get("tables", {}).get("TREE_NODES", [])
            areas = [n for n in nodes
                     if str(n.get("OBJECTNAME", "")).strip().lower() == "r3syslog"
                     and str(n.get("MTCLASS", "")).strip() == "101"]
            if not areas:
                return {"source": "live_sap", "total_entries": 0, "by_severity": {}, "entries": [],
                        "note": "No R3Syslog message-log nodes found in the CCMS tree.",
                        "hours_back": hours_back}

            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=hours_back)
            ts = lambda dt: dt.strftime("%Y%m%d%H%M%S")

            # 2. one stateful batch: logon, then GETMLHIS for every area.
            calls = [logon]
            for a in areas:
                calls.append({"function": "BAPI_SYSTEM_MTE_GETMLHIS",
                              "params": {"EXTERNAL_USER_NAME": external_user_name,
                                         "TID": {f: a.get(f, "") for f in tid_fields},
                                         "START_TIMESTAMP": ts(start), "END_TIMESTAMP": ts(now)}})
            hist = await connector._rfc_batch(calls)
            if not hist.get("success"):
                return {"source": "live_sap", "total_entries": 0, "by_severity": {}, "entries": [],
                        "note": "CCMS message-log read failed: " + str(hist.get("error")),
                        "hours_back": hours_back}

            def value_label(v):
                return {"1": "info", "2": "warning", "3": "error"}.get(str(v).strip(), "info")

            want = {"error": {"error"}, "warning": {"warning"},
                    "info": {"info"}}.get(severity_filter.lower())

            from collections import Counter
            by_sev = Counter()
            by_area = Counter()
            entries = []
            for a, res in zip(areas, hist["results"][1:]):
                if not isinstance(res, dict):
                    continue
                lines = res.get("tables", {}).get("MSG_LINE_DATA", [])
                raw = res.get("tables", {}).get("XMI_MSG_RAW", [])
                ext = res.get("tables", {}).get("XMI_MSG_EXT", [])
                area = str(a.get("MTNAMESHRT", "")).strip()
                for i, ln in enumerate(lines):
                    label = value_label(ln.get("VALUEFLTRD", ln.get("VALUEORIG")))
                    by_sev[label] += 1
                    by_area[area] += 1
                    if want and label not in want:
                        continue
                    msg_id = raw[i].get("MSGID", "") if i < len(raw) else ""
                    text = ext[i].get("MSG", "") if i < len(ext) else ""
                    entries.append({
                        "date": ln.get("MSCDATE", ""),
                        "time": ln.get("MSCTIME", ""),
                        "area": area,
                        "severity": label,
                        "severity_value": str(ln.get("SEVERORIG", "")).strip(),
                        "message_id": str(msg_id).strip(),
                        "text": str(text).strip()[:160],
                        "user": str(ln.get("USERID", "")).strip(),
                    })
            entries.sort(key=lambda e: (e["date"], e["time"]), reverse=True)
            total_matched = len(entries)

            return {
                "source": "live_sap",
                "hours_back": hours_back,
                "severity_filter": severity_filter,
                "total_entries": sum(by_sev.values()),
                "matched_entries": total_matched,
                "by_severity": dict(by_sev),
                "by_area": dict(by_area.most_common(10)),
                "entries": entries[:max_entries],
                "collector": "CCMS R3Syslog via BAPI_SYSTEM_MTE_GETMLHIS",
                "limitations": "reads the CCMS syslog collector (R3Syslog MTEs), not the raw syslog files; coverage depends on CCMS retention and which categories the collector holds, and the Security category may be sparse.",
                "checked_at": now.isoformat(),
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
