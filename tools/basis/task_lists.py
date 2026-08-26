"""Task list monitoring tools – 3 tools registered via register_task_list_tools().

Covers: STC01 (task list browser), STC01 detail view, STC02 (execution history).
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta

logger = logging.getLogger("syntaai.tools.basis.task_lists")

# Status mapping for STC_SESSION.STATUS (SAP F4 values)
_STATUS_MAP = {
    "01": "Waiting to be executed",
    "02": "Running",
    "03": "Stopped",
    "04": "Manual activities required",
    "05": "Does not need to be executed",
    "06": "Execution scheduled",
    "10": "Finished successfully",
    "11": "Finished with warnings",
    "12": "Errors occurred",
    "13": "Aborted",
}

_FINALIZED_MAP = {
    "": "Not finalized",
    "X": "Finalized",
    "U": "Finalized by User",
}


def register_task_list_tools(mcp, connector):
    """Register all task list monitoring tools on the given FastMCP instance."""

    # ------------------------------------------------------------------
    # Helper: read a table with graceful failure
    # ------------------------------------------------------------------
    async def _safe_table_read(table, fields, where="", max_rows=500):
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
    # 1. get_task_lists
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_task_lists(
        filter: str = "",
        include_obsolete: bool = False,
    ) -> dict:
        """List available SAP technical configuration task lists with descriptions and task count (STC01). Use to see what automated configuration checks exist in the system."""
        try:
            # Build WHERE clause for headers
            where_parts = []
            if filter:
                where_parts.append(f"SCENARIO_ID LIKE '{filter}%'")
            if not include_obsolete:
                where_parts.append("OBSOLETE NE 'X'")
            hdr_where = " AND ".join(where_parts)

            # Fetch all 4 tables in parallel
            hdr_task = _safe_table_read(
                "STC_SCN_HDR",
                ["SCENARIO_ID", "CREATED_ON", "CREATED_BY", "CHANGED_ON", "CHANGED_BY",
                 "BUILD_NO", "OBSOLETE", "BASIC_SCEN_ID", "CONFIG_CLASS"],
                hdr_where,
            )
            descr_task = _safe_table_read(
                "STC_SCN_HDR_T",
                ["SCENARIO_ID", "DESCR"],
                "LANGU = 'E'",
            )
            tasks_task = _safe_table_read(
                "STC_SCN_TASKS",
                ["SCENARIO_ID", "SEQUENCE"],
            )
            group_task = _safe_table_read(
                "STC_BASSCN_HDR_T",
                ["BASIC_SCEN_ID", "DESCR"],
                "LANGU = 'E'",
            )

            (headers, hdr_err), (descriptions, _), (tasks, _), (groups, _) = await asyncio.gather(
                hdr_task, descr_task, tasks_task, group_task
            )

            if hdr_err:
                return {
                    "source": "live_sap",
                    "error": "Could not read STC_SCN_HDR table. JCo connector required.",
                    "tool": "get_task_lists",
                }

            # Build lookup maps
            descr_map = {r.get("SCENARIO_ID", ""): r.get("DESCR", "") for r in descriptions}
            group_map = {r.get("BASIC_SCEN_ID", ""): r.get("DESCR", "") for r in groups}

            # Count tasks per scenario
            task_counts: Counter = Counter()
            for t in tasks:
                task_counts[t.get("SCENARIO_ID", "")] += 1

            # Build result list
            task_lists = []
            for h in headers:
                sid = h.get("SCENARIO_ID", "")
                group_id = h.get("BASIC_SCEN_ID", "")
                task_lists.append({
                    "scenario_id": sid,
                    "description": descr_map.get(sid, ""),
                    "group": group_id,
                    "group_description": group_map.get(group_id, ""),
                    "config_class": h.get("CONFIG_CLASS", ""),
                    "build_number": h.get("BUILD_NO", ""),
                    "task_count": task_counts.get(sid, 0),
                    "created_by": h.get("CREATED_BY", ""),
                    "created_on": h.get("CREATED_ON", ""),
                    "changed_on": h.get("CHANGED_ON", ""),
                    "obsolete": h.get("OBSOLETE", "") == "X",
                })

            return {
                "source": "live_sap",
                "total_task_lists": len(task_lists),
                "task_lists": task_lists,
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_task_lists"}

    # ------------------------------------------------------------------
    # 2. get_task_list_details
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_task_list_details(scenario_id: str) -> dict:
        """Show all tasks/steps within a specific SAP task list including execution sequence, task types, and phases (STC01 detail view)."""
        try:
            # Fetch all 5 tables in parallel
            hdr_task = _safe_table_read(
                "STC_SCN_HDR",
                ["SCENARIO_ID", "CREATED_ON", "CREATED_BY", "CHANGED_ON", "CHANGED_BY",
                 "BUILD_NO", "OBSOLETE", "BASIC_SCEN_ID"],
                f"SCENARIO_ID = '{scenario_id}'",
                1,
            )
            descr_task = _safe_table_read(
                "STC_SCN_HDR_T",
                ["DESCR"],
                f"SCENARIO_ID = '{scenario_id}' AND LANGU = 'E'",
                1,
            )
            tasks_task = _safe_table_read(
                "STC_SCN_TASKS",
                ["SCENARIO_ID", "SEQUENCE", "TASKTYPE", "TASKNAME", "PHASE"],
                f"SCENARIO_ID = '{scenario_id}'",
            )
            attr_task = _safe_table_read(
                "STC_SCN_ATTR",
                ["ATTR_NAME", "ATTR_VALUE"],
                f"SCENARIO_ID = '{scenario_id}'",
            )
            variant_task = _safe_table_read(
                "STC_TEMPLATE",
                ["TEMPLATE_ID", "CREATED_BY", "CREATED_ON"],
                f"SCENARIO_ID = '{scenario_id}'",
            )

            (headers, hdr_err), (descriptions, _), (tasks, _), (attrs, _), (variants, _) = await asyncio.gather(
                hdr_task, descr_task, tasks_task, attr_task, variant_task
            )

            if hdr_err:
                return {
                    "source": "live_sap",
                    "error": "Could not read STC_SCN_HDR table. JCo connector required.",
                    "tool": "get_task_list_details",
                }

            if not headers:
                return {
                    "source": "live_sap",
                    "error": f"Task list '{scenario_id}' not found.",
                    "tool": "get_task_list_details",
                }

            hdr = headers[0]
            description = descriptions[0].get("DESCR", "") if descriptions else ""

            # Sort tasks by sequence
            sorted_tasks = sorted(tasks, key=lambda t: int(t.get("SEQUENCE", 0)))
            task_list = [
                {
                    "sequence": t.get("SEQUENCE", ""),
                    "task_name": t.get("TASKNAME", ""),
                    "task_type": t.get("TASKTYPE", ""),
                    "phase": t.get("PHASE", ""),
                }
                for t in sorted_tasks
            ]

            # Build attributes dict
            attributes = {a.get("ATTR_NAME", ""): a.get("ATTR_VALUE", "") for a in attrs}

            # Build variants list
            variant_list = [
                {
                    "template_id": v.get("TEMPLATE_ID", ""),
                    "created_by": v.get("CREATED_BY", ""),
                    "created_on": v.get("CREATED_ON", ""),
                }
                for v in variants
            ]

            return {
                "source": "live_sap",
                "scenario_id": scenario_id,
                "description": description,
                "group": hdr.get("BASIC_SCEN_ID", ""),
                "build_number": hdr.get("BUILD_NO", ""),
                "created_by": hdr.get("CREATED_BY", ""),
                "created_on": hdr.get("CREATED_ON", ""),
                "changed_on": hdr.get("CHANGED_ON", ""),
                "obsolete": hdr.get("OBSOLETE", "") == "X",
                "total_tasks": len(task_list),
                "tasks": task_list,
                "attributes": attributes,
                "variants": variant_list,
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_task_list_details"}

    # ------------------------------------------------------------------
    # 3. get_task_list_runs
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_task_list_runs(
        scenario_id: str = "",
        days_back: int = 30,
        status_filter: str = "all",
    ) -> dict:
        """Show SAP task list execution history — who ran which task list, when, and overall status (STC02 equivalent). Shows run-level results; per-task details are in the serialized CONTENT blob."""
        try:
            days_back = min(max(days_back, 1), 3650)

            # Calculate cutoff timestamp (YYYYMMDDHHMMSS)
            cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d%H%M%S")

            # Build WHERE clause for STC_SESSION
            where_parts = [f"CREATED_ON GE '{cutoff}'"]
            if scenario_id:
                where_parts.append(f"SCENARIO_ID = '{scenario_id}'")

            status_filter_map = {
                "success": "10",
                "error": "12",
                "warning": "11",
                "running": "02",
            }
            if status_filter in status_filter_map:
                where_parts.append(f"STATUS = '{status_filter_map[status_filter]}'")

            session_where = " AND ".join(where_parts)

            # Fetch session data and descriptions in parallel
            session_task = _safe_table_read(
                "STC_SESSION",
                ["SESSION_ID", "SCENARIO_ID", "TEMPLATE_ID", "STATUS", "FINALIZED",
                 "CREATED_BY", "CREATED_ON", "CHANGED_BY", "CHANGED_ON",
                 "USERNAME", "SID", "SYSNR", "MANDT", "HOST"],
                session_where,
            )
            descr_task = _safe_table_read(
                "STC_SCN_HDR_T",
                ["SCENARIO_ID", "DESCR"],
                "LANGU = 'E'",
            )

            (sessions, sess_err), (descriptions, _) = await asyncio.gather(
                session_task, descr_task
            )

            if sess_err:
                return {
                    "source": "live_sap",
                    "error": "Could not read STC_SESSION table. JCo connector required.",
                    "tool": "get_task_list_runs",
                }

            descr_map = {r.get("SCENARIO_ID", ""): r.get("DESCR", "") for r in descriptions}

            # Build summary counts
            summary_counter: Counter = Counter()
            summary_labels = {
                "10": "finished_successfully",
                "11": "finished_with_warnings",
                "12": "errors_occurred",
                "02": "running",
                "01": "waiting",
                "03": "stopped",
                "04": "manual_activities_required",
                "05": "does_not_need_execution",
                "06": "execution_scheduled",
                "13": "aborted",
            }

            runs = []
            for s in sessions:
                status = s.get("STATUS", "")
                finalized = s.get("FINALIZED", "")
                sid = s.get("SCENARIO_ID", "")

                label = summary_labels.get(status, f"unknown_{status}")
                summary_counter[label] += 1

                runs.append({
                    "session_id": s.get("SESSION_ID", ""),
                    "scenario_id": sid,
                    "description": descr_map.get(sid, ""),
                    "template_id": s.get("TEMPLATE_ID", ""),
                    "status": status,
                    "status_text": _STATUS_MAP.get(status, f"Unknown ({status})"),
                    "finalized": finalized,
                    "finalized_text": _FINALIZED_MAP.get(finalized, f"Unknown ({finalized})"),
                    "username": s.get("USERNAME", ""),
                    "created_by": s.get("CREATED_BY", ""),
                    "created_on": s.get("CREATED_ON", ""),
                    "changed_on": s.get("CHANGED_ON", ""),
                    "system_id": s.get("SID", ""),
                    "system_number": s.get("SYSNR", ""),
                    "client": s.get("MANDT", ""),
                    "host": s.get("HOST", ""),
                })

            # Sort by created_on descending (most recent first)
            runs.sort(key=lambda r: r.get("created_on", ""), reverse=True)

            return {
                "source": "live_sap",
                "total_runs": len(runs),
                "days_searched": days_back,
                "summary": dict(summary_counter),
                "runs": runs,
            }
        except Exception as e:
            return {"source": "live_sap", "error": str(e), "tool": "get_task_list_runs"}
