"""System administration tools – 3 tools registered via register_system_tools().

Covers: background jobs (SM37), client settings (T000) and SNC.

Licence usage (USMM) is deliberately absent from this build: it reads the user
master, which is outside the scope of a Basis-only tool set.
"""

import logging

logger = logging.getLogger("syntaai.tools.basis.system")


def register_system_tools(mcp, connector):
    """Register all system administration tools on the given FastMCP instance."""

    async def _safe_table_read(table, fields, where="", max_rows=100):
        if not hasattr(connector, "_table_read"):
            return [], True
        try:
            rows = await connector._table_read(table, fields=fields, where=where, max_rows=max_rows)
            return rows, False
        except Exception as exc:
            logger.warning("%s table read failed: %s", table, exc)
            return [], True

    # ------------------------------------------------------------------
    # 1. get_background_jobs
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_background_jobs(status: str = "all", max_rows: int = 200) -> dict:
        """List SAP background jobs (SM37). Filter by status: all, active, finished, cancelled.

        Reads TBTCO (the job header table). Job status codes: R active, F finished,
        A cancelled, S released, Y ready, P scheduled.

        Args:
            status: all | active | finished | cancelled (default all).
            max_rows: cap on jobs read (default 200).
        """
        try:
            max_rows = min(max(int(max_rows), 1), 2000)
            status_map = {"active": "R", "finished": "F", "cancelled": "A"}
            where = ""
            code = status_map.get(status.lower())
            if code:
                where = f"STATUS = '{code}'"
            rows, failed = await _safe_table_read(
                "TBTCO",
                ["JOBNAME", "JOBCOUNT", "STATUS", "SDLSTRTDT", "SDLSTRTTM",
                 "STRTDATE", "STRTTIME", "ENDDATE", "ENDTIME", "SDLUNAME"],
                where, max_rows,
            )
            if failed:
                return {"source": "live_sap", "error": "Could not read TBTCO (job header table).",
                        "tool": "get_background_jobs", "total": 0, "jobs": []}

            labels = {"R": "active", "F": "finished", "A": "cancelled",
                      "S": "released", "Y": "ready", "P": "scheduled"}
            from collections import Counter
            by_status = Counter()
            jobs = []
            for r in rows:
                st = r.get("STATUS", "")
                by_status[labels.get(st, st or "unknown")] += 1
                jobs.append({
                    "job_name": r.get("JOBNAME", ""),
                    "job_count": r.get("JOBCOUNT", ""),
                    "status": st,
                    "status_label": labels.get(st, "unknown"),
                    "scheduled_start": (r.get("SDLSTRTDT", "") + r.get("SDLSTRTTM", "")).strip(),
                    "started": (r.get("STRTDATE", "") + r.get("STRTTIME", "")).strip(),
                    "ended": (r.get("ENDDATE", "") + r.get("ENDTIME", "")).strip(),
                    "created_by": r.get("SDLUNAME", ""),
                })
            # Show the cancelled ones first: they are what an admin looks for in SM37.
            jobs.sort(key=lambda j: (j["status"] != "A", j["job_name"]))
            return {
                "source": "live_sap",
                "status_filter": status,
                "total": len(jobs),
                "by_status": dict(by_status),
                "cancelled_count": by_status.get("cancelled", 0),
                "jobs": jobs[:20],
            }
        except Exception as e:
            logger.error("get_background_jobs failed: %s", e)
            return {"source": "live_sap", "error": str(e), "total": 0, "jobs": []}

    # 2. get_client_settings
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_client_settings() -> dict:
        """Check SAP client settings (T000) for cross-client access and change recording configuration."""
        try:
            if hasattr(connector, '_table_read'):
                clients = await connector._table_read(
                    "T000",
                    fields=["MANDT", "MTEXT", "ORT01", "CCCATEGORY", "CCCORACTIV", "CCNOCLIIND", "CCCOPYLOCK", "CCORIGCONT"],
                )
                findings = []
                for c in clients:
                    client_id = c.get("MANDT", "")
                    # CCNOCLIIND: 0 = cross-client changes allowed (risk!)
                    if str(c.get("CCNOCLIIND", "")) == "0":
                        findings.append({"client": client_id, "issue": "Cross-client changes allowed", "severity": "HIGH"})
                    # CCCATEGORY: P=Production, T=Test, C=Customizing
                    if str(c.get("CCCATEGORY", "")) == "P" and str(c.get("CCCORACTIV", "")) != "1":
                        findings.append({"client": client_id, "issue": "Production client not protected against changes", "severity": "CRITICAL"})
                return {
                    "source": "live_sap",
                    "total_clients": len(clients),
                    "clients": clients,
                    "findings": findings,
                    "risk": "HIGH" if findings else "LOW",
                }
            return {"source": "live_sap", "error": "JCo connector required for deep table access"}
        except Exception as e:
            logger.error("get_client_settings failed: %s", e)
            return {"source": "live_sap", "error": str(e), "total_clients": 0, "clients": []}

    # ------------------------------------------------------------------
    # 3. check_snc_configuration
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def check_snc_configuration() -> dict:
        """Check Secure Network Communication (SNC) configuration status for encrypted SAP connections."""
        try:
            if hasattr(connector, '_table_read'):
                # Check PAHI for SNC-related profile parameters
                snc_params = await connector._table_read(
                    "PAHI",
                    fields=["PARNAME", "PARVALUE"],
                    where="PARNAME LIKE 'snc%'",
                )
                snc_enabled = any(p.get("PARNAME", "") == "snc/enable" and str(p.get("PARVALUE", "0")) == "1" for p in snc_params)
                return {
                    "source": "live_sap",
                    "snc_enabled": snc_enabled,
                    "snc_parameters": snc_params,
                    "risk": "LOW" if snc_enabled else "HIGH",
                    "recommendation": "Enable SNC for encrypted RFC and dialog connections" if not snc_enabled else "SNC is enabled",
                }
            return {"source": "live_sap", "error": "JCo connector required for deep table access"}
        except Exception as e:
            logger.error("check_snc_configuration failed: %s", e)
            return {"source": "live_sap", "error": str(e), "snc_enabled": False}
