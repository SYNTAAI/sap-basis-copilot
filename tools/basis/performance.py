"""Performance analysis tools – 6 tools registered via register_performance_tools().

SAP equivalents: SM50, SM66, ST05, ST02, RZ10, ST10.
Covers: work processes, response times, DB performance, memory, parameters, buffers.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

logger = logging.getLogger("syntaai.tools.basis.performance")

# Work process status mapping
_WP_STATUS_MAP = {
    "Run": "running",
    "Running": "running",
    "Wait": "waiting",
    "Waiting": "waiting",
    "Hold": "stopped",
    "Stopped": "stopped",
    "Ended": "ended",
}

# Work process type mapping
_WP_TYPE_MAP = {
    "DIA": "Dialog",
    "BTC": "Background",
    "UPD": "Update",
    "ENQ": "Enqueue",
    "SPO": "Spool",
    "UP2": "Update2",
}

# Task type mapping for response time analysis
_TASK_TYPE_MAP = {
    "dialog": "DIA",
    "batch": "BTC",
    "rfc": "RFC",
    "update": "UPD",
}

# Best-practice parameter definitions: (best_value, comparison, category)
_PARAM_RECOMMENDATIONS = {
    "login/fails_to_session_end":       ("3",       "lte", "security"),
    "login/fails_to_user_lock":         ("5",       "lte", "security"),
    "login/min_password_lng":           ("8",       "gte", "security"),
    "login/password_expiration_time":   ("90",      "lte", "security"),
    "auth/rfc_authority_check":         ("1",       "eq",  "security"),
    "rdisp/gui_auto_logout":            ("900",     "lte", "security"),
    "login/no_automatic_user_sapstar":  ("1",       "eq",  "security"),
    "rdisp/max_wprun_time":             ("600",     "lte", "performance"),
    "rdisp/wp_no_dia":                  ("10",      "gte", "performance"),
    "rdisp/wp_no_btc":                  ("3",       "gte", "performance"),
    "icm/max_conn":                     ("500",     "gte", "performance"),
    "em/initial_size_MB":               ("2000",    "gte", "memory"),
    "ztta/roll_extension_dia":          ("2097152", "gte", "memory"),
    "abap/buffersize":                  ("500000",  "gte", "memory"),
}

# Buffer health classification thresholds
_BUFFER_HEALTH_THRESHOLDS = [
    (98.0, "excellent"),
    (95.0, "good"),
    (90.0, "acceptable"),
    (80.0, "poor"),
]


def _classify_buffer_health(hit_ratio_pct: float) -> str:
    for threshold, label in _BUFFER_HEALTH_THRESHOLDS:
        if hit_ratio_pct >= threshold:
            return label
    return "critical"


def _check_compliance(actual_value: str, best_practice: str, comparison: str):
    """Check if actual_value meets best_practice per comparison rule. Returns bool or None."""
    try:
        a, b = float(actual_value), float(best_practice)
        if comparison == "lte":
            return a <= b
        if comparison == "gte":
            return a >= b
        if comparison == "eq":
            return a == b
    except (ValueError, TypeError):
        return None


def register_performance_tools(mcp, connector):
    """Register all performance analysis tools on the given FastMCP instance."""

    # ------------------------------------------------------------------
    # Helper: read a table with graceful failure
    # ------------------------------------------------------------------
    async def _safe_table_read(table, fields, where="", max_rows=100):
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
    # Helper: call an RFC function module with graceful failure
    # ------------------------------------------------------------------
    async def _safe_rfc_call(func_name, params=None):
        """Call an RFC function module, returning (result, error_flag). Never raises."""
        if not hasattr(connector, "_rfc_call"):
            return None, True
        try:
            result = await connector._rfc_call(func_name, params)
            return result, False
        except Exception as exc:
            logger.warning("%s RFC call failed: %s", func_name, exc)
            return None, True

    # ------------------------------------------------------------------
    # 1. get_work_process_status
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_work_process_status() -> dict:
        """Current work process status across all instances — active, waiting, stopped (SM50/SM66)."""
        try:
            rows = []
            failed = True

            # Primary: use RFC TH_WPINFO (SAPWP table does not exist)
            # Response format: {"export": {...}, "tables": {"WPLIST": [...]}}
            rfc_result, rfc_failed = await _safe_rfc_call("TH_WPINFO")
            if not rfc_failed and rfc_result is not None:
                if isinstance(rfc_result, dict):
                    tables = rfc_result.get("tables", {})
                    rows = tables.get("WPLIST", rfc_result.get("WPLIST", []))
                elif isinstance(rfc_result, list):
                    rows = rfc_result
                failed = not bool(rows)

            if failed:
                return {
                    "source": "live_sap",
                    "error": "Could not retrieve work process data. "
                             "TH_WPINFO RFC call failed. "
                             "Use transaction SM50 or SM66 in the SAP GUI to view work process status.",
                    "tool": "get_work_process_status",
                }

            by_status: Counter = Counter()
            by_type: Counter = Counter()
            work_processes = []

            for row in rows:
                raw_status = row.get("STATUS", row.get("WP_STATUS", ""))
                raw_type = row.get("TYP", row.get("WP_TYP", ""))
                mapped_status = _WP_STATUS_MAP.get(raw_status, raw_status)
                mapped_type = _WP_TYPE_MAP.get(raw_type, raw_type)

                by_status[mapped_status] += 1
                by_type[mapped_type] += 1

                work_processes.append({
                    "instance": row.get("INAME", row.get("WP_INAME", "")),
                    "wp_number": row.get("WPNO", row.get("WP_NO", "")),
                    "type": mapped_type,
                    "type_raw": raw_type,
                    "status": mapped_status,
                    "status_raw": raw_status,
                    "report": row.get("REPORT", row.get("WP_REPORT", "")),
                    "user": row.get("USER", row.get("WP_USER", "")),
                    "elapsed_time": row.get("TIME", row.get("WP_ELTIME", "")),
                    "cpu_time": row.get("CPU", row.get("WP_CPUTIME", "")),
                })

            return {
                "source": "live_sap",
                "data_method": "RFC TH_WPINFO",
                "total_work_processes": len(work_processes),
                "by_status": dict(by_status),
                "by_type": dict(by_type),
                "stopped_count": by_status.get("stopped", 0),
                "work_processes": work_processes,
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_work_process_status"}

    # ------------------------------------------------------------------
    # 2. get_response_time_analysis
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_response_time_analysis(
        hours_back: int = 1,
        task_type: str = "all",
    ) -> dict:
        """Dialog, batch, and RFC response time statistics from SAP workload monitor (ST03N)."""
        try:
            hours_back = min(max(hours_back, 1), 24)

            # The MONI table causes DATA_BUFFER_EXCEEDED and SDBAD has no usable data.
            # These workload statistics are not reliably accessible via RFC_READ_TABLE.
            # Try TH_WPINFO as a lightweight alternative to get current snapshot.
            rfc_result, rfc_failed = await _safe_rfc_call("TH_WPINFO")

            if not rfc_failed and rfc_result is not None:
                if isinstance(rfc_result, list):
                    wp_rows = rfc_result
                elif isinstance(rfc_result, dict):
                    tables = rfc_result.get("tables", {})
                    wp_rows = tables.get("WPLIST", rfc_result.get("WPLIST", []))
                else:
                    wp_rows = []

                # Summarize current work process activity as a proxy for response time analysis
                by_type: Counter = Counter()
                active_count = 0
                for row in wp_rows:
                    raw_type = row.get("TYP", row.get("WP_TYP", ""))
                    raw_status = row.get("STATUS", row.get("WP_STATUS", ""))
                    mapped_type = _WP_TYPE_MAP.get(raw_type, raw_type)
                    by_type[mapped_type] += 1
                    if _WP_STATUS_MAP.get(raw_status, raw_status) == "running":
                        active_count += 1

                return {
                    "source": "live_sap",
                    "data_method": "RFC TH_WPINFO (current snapshot)",
                    "hours_back": hours_back,
                    "task_type_filter": task_type,
                    "note": "Historical response time statistics (MONI/SDBAD tables) are not available "
                            "via RFC_READ_TABLE due to DATA_BUFFER_EXCEEDED or missing data. "
                            "Showing current work process snapshot instead. "
                            "For detailed historical response time analysis, use transaction ST03N in SAP GUI.",
                    "current_snapshot": {
                        "total_work_processes": len(wp_rows),
                        "active_running": active_count,
                        "by_type": dict(by_type),
                    },
                    "recommendation": "Use SAP transaction ST03N for historical workload and response time analysis.",
                }

            return {
                "source": "live_sap",
                "hours_back": hours_back,
                "task_type_filter": task_type,
                "error": "Response time statistics are not available via RFC_READ_TABLE. "
                         "The MONI table causes DATA_BUFFER_EXCEEDED errors and SDBAD has no usable data.",
                "recommendation": "Use SAP transaction ST03N for workload and response time analysis.",
                "tool": "get_response_time_analysis",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_response_time_analysis"}

    # ------------------------------------------------------------------
    # 3. get_database_performance
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_database_performance(
        top_n: int = 20,
        min_executions: int = 5,
    ) -> dict:
        """Database object statistics from DBSTATC and general DB info (ST05/DB02)."""
        try:
            top_n = min(max(top_n, 1), 50)

            # DBSTATC actual fields: DBOBJ, DOTYP, OBJOW, DBTYP, VWTYP, ACTIV, OBJEC,
            #   AEDAT, SIGNI, AMETH, OPTIO, TOBDO, HISTO, TDDAT, DURAT, PLAND
            stmt_rows, stmt_failed = await _safe_table_read(
                "DBSTATC",
                ["DBOBJ", "DOTYP", "OBJOW", "DBTYP", "ACTIV", "OBJEC", "AEDAT", "SIGNI", "AMETH", "DURAT"],
                "",
                top_n,
            )

            if stmt_failed:
                return {
                    "source": "live_sap",
                    "top_n": top_n,
                    "min_executions_filter": min_executions,
                    "db_stats_available": False,
                    "expensive_statements": [],
                    "note": "Could not read DBSTATC table. DB performance statistics may require "
                            "specific authorizations. Use SAP transaction ST05 (SQL Trace) or "
                            "DB02 (Database Administration) for detailed database performance analysis.",
                    "recommendation": "Use SAP transactions ST05, DB02, or DBACOCKPIT for database performance monitoring.",
                }

            # Parse DBSTATC entries (these are DB object statistics, not SQL statements)
            db_objects = []
            for row in stmt_rows:
                db_objects.append({
                    "db_object": row.get("DBOBJ", ""),
                    "object_type": row.get("DOTYP", ""),
                    "owner": row.get("OBJOW", ""),
                    "db_type": row.get("DBTYP", ""),
                    "active": row.get("ACTIV", ""),
                    "object": row.get("OBJEC", ""),
                    "last_changed": row.get("AEDAT", ""),
                    "significance": row.get("SIGNI", ""),
                    "analysis_method": row.get("AMETH", ""),
                    "duration": row.get("DURAT", ""),
                })

            return {
                "source": "live_sap",
                "data_method": "DBSTATC table read",
                "top_n": top_n,
                "db_stats_available": True,
                "total_objects": len(db_objects),
                "db_objects": db_objects,
                "note": "DBSTATC contains database object statistics configuration, not live SQL traces. "
                        "For expensive SQL analysis, use SAP transaction ST05 (SQL Trace) or DBACOCKPIT.",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_database_performance"}

    # ------------------------------------------------------------------
    # 4. get_memory_usage
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_memory_usage() -> dict:
        """Current memory allocation vs configured limits per instance (ST02)."""
        try:
            # RSSTAT10, EUFRAWDATA, and SMTXCOUNT tables do not exist in this system.
            # Memory and buffer statistics are not available via standard table reads.
            # Try TH_WPINFO for at least work process memory info.
            rfc_result, rfc_failed = await _safe_rfc_call("TH_WPINFO")

            if not rfc_failed and rfc_result is not None:
                if isinstance(rfc_result, list):
                    wp_rows = rfc_result
                elif isinstance(rfc_result, dict):
                    tables = rfc_result.get("tables", {})
                    wp_rows = tables.get("WPLIST", rfc_result.get("WPLIST", []))
                else:
                    wp_rows = []

                # Summarize work process resource usage as a proxy
                by_type: Counter = Counter()
                running_count = 0
                for row in wp_rows:
                    raw_type = row.get("TYP", row.get("WP_TYP", ""))
                    raw_status = row.get("STATUS", row.get("WP_STATUS", ""))
                    mapped_type = _WP_TYPE_MAP.get(raw_type, raw_type)
                    by_type[mapped_type] += 1
                    if _WP_STATUS_MAP.get(raw_status, raw_status) == "running":
                        running_count += 1

                return {
                    "source": "live_sap",
                    "data_method": "RFC TH_WPINFO (work process snapshot)",
                    "note": "Detailed memory and buffer statistics (RSSTAT10, EUFRAWDATA, SMTXCOUNT) "
                            "are not available via RFC_READ_TABLE as these tables do not exist. "
                            "Showing work process summary instead. "
                            "For detailed memory analysis, use transaction ST02 in SAP GUI.",
                    "work_process_summary": {
                        "total_work_processes": len(wp_rows),
                        "running": running_count,
                        "by_type": dict(by_type),
                    },
                    "recommendation": "Use SAP transaction ST02 for detailed memory and buffer usage analysis.",
                }

            return {
                "source": "live_sap",
                "error": "Memory and buffer statistics are not available via RFC_READ_TABLE. "
                         "The tables RSSTAT10, EUFRAWDATA, and SMTXCOUNT do not exist in this system.",
                "recommendation": "Use SAP transaction ST02 for memory and buffer usage analysis.",
                "tool": "get_memory_usage",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_memory_usage"}

    # ------------------------------------------------------------------
    # 5. get_parameter_recommendations
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_parameter_recommendations(
        category: str = "all",
    ) -> dict:
        """Compare key SAP profile parameters against security/performance best practices (RZ10/RZ11)."""
        try:
            param_names = list(_PARAM_RECOMMENDATIONS.keys())

            # PAHI uses fields PARNAME and PARVALUE (not NAME/VALUE).
            # Query parameters individually to avoid exceeding RFC_READ_TABLE's
            # 72-character WHERE clause line limit.
            current_values = {}
            any_success = False

            for param_name in param_names:
                where_clause = f"PARNAME = '{param_name}'"
                rows, failed = await _safe_table_read(
                    "PAHI",
                    ["PARNAME", "PARVALUE"],
                    where_clause,
                    10,
                )
                if not failed:
                    any_success = True
                    for row in rows:
                        name = row.get("PARNAME", "").strip()
                        if name:
                            current_values[name] = row.get("PARVALUE", "").strip()

            parameters = []
            compliant_count = 0
            non_compliant_count = 0
            unknown_count = 0

            for param_name, (best_val, comparison, cat) in _PARAM_RECOMMENDATIONS.items():
                if category != "all" and cat != category:
                    continue

                found = param_name in current_values
                current_val = current_values.get(param_name, "")

                if found and current_val:
                    is_compliant = _check_compliance(current_val, best_val, comparison)
                    if is_compliant is True:
                        compliant_count += 1
                    elif is_compliant is False:
                        non_compliant_count += 1
                    else:
                        unknown_count += 1
                else:
                    is_compliant = None
                    unknown_count += 1

                parameters.append({
                    "name": param_name,
                    "current_value": current_val,
                    "best_practice_value": best_val,
                    "category": cat,
                    "is_compliant": is_compliant,
                    "comparison_rule": comparison,
                    "found_in_system": found,
                })

            note = ""
            if not any_success:
                note = ("Could not read PAHI table. Check authorizations. "
                        "Use SAP transaction RZ10 or RZ11 to review profile parameters.")

            result = {
                "source": "live_sap",
                "data_method": "PAHI table (PARNAME/PARVALUE fields)",
                "category_filter": category,
                "total_parameters_checked": len(parameters),
                "compliant_count": compliant_count,
                "non_compliant_count": non_compliant_count,
                "unknown_count": unknown_count,
                "parameters": parameters,
            }
            if note:
                result["note"] = note
            return result
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_parameter_recommendations"}

    # ------------------------------------------------------------------
    # 6. analyze_buffer_performance
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def analyze_buffer_performance(
        alert_threshold_pct: float = 90.0,
    ) -> dict:
        """SAP buffer hit ratios and cache efficiency analysis (ST02)."""
        try:
            # RSSTAT10 and BUFSTAT tables do not exist in this system.
            # Buffer statistics are not available via standard table reads.
            # Try TH_WPINFO for a basic work process snapshot.
            rfc_result, rfc_failed = await _safe_rfc_call("TH_WPINFO")

            if not rfc_failed and rfc_result is not None:
                if isinstance(rfc_result, list):
                    wp_rows = rfc_result
                elif isinstance(rfc_result, dict):
                    tables = rfc_result.get("tables", {})
                    wp_rows = tables.get("WPLIST", rfc_result.get("WPLIST", []))
                else:
                    wp_rows = []

                by_type: Counter = Counter()
                running_count = 0
                for row in wp_rows:
                    raw_type = row.get("TYP", row.get("WP_TYP", ""))
                    raw_status = row.get("STATUS", row.get("WP_STATUS", ""))
                    mapped_type = _WP_TYPE_MAP.get(raw_type, raw_type)
                    by_type[mapped_type] += 1
                    if _WP_STATUS_MAP.get(raw_status, raw_status) == "running":
                        running_count += 1

                return {
                    "source": "live_sap",
                    "data_method": "RFC TH_WPINFO (work process snapshot)",
                    "alert_threshold_pct": alert_threshold_pct,
                    "note": "Buffer statistics (RSSTAT10, BUFSTAT) are not available via RFC_READ_TABLE "
                            "as these tables do not exist. Showing work process summary instead. "
                            "For detailed buffer hit ratios and cache analysis, use transaction ST02 in SAP GUI.",
                    "work_process_summary": {
                        "total_work_processes": len(wp_rows),
                        "running": running_count,
                        "by_type": dict(by_type),
                    },
                    "recommendation": "Use SAP transaction ST02 for buffer hit ratios and cache efficiency analysis.",
                }

            return {
                "source": "live_sap",
                "alert_threshold_pct": alert_threshold_pct,
                "error": "Buffer statistics are not available via RFC_READ_TABLE. "
                         "The tables RSSTAT10 and BUFSTAT do not exist in this system.",
                "recommendation": "Use SAP transaction ST02 for buffer hit ratios and cache efficiency analysis.",
                "tool": "analyze_buffer_performance",
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "analyze_buffer_performance"}
