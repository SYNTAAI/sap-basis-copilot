"""Operating-system layer tools – 1 tool registered via register_os_monitor_tools().

Covers: ST06 / OS07N (host CPU, memory, paging, swap, filesystems) via CCMS over
RFC. No SSH and no shell access is involved.

CCMS's XAL interface needs a stateful RFC session: BAPI_XMI_LOGON establishes it
and every following call must land in the same ABAP session. That is why this
module uses the bridge's /api/rfc/batch endpoint (connector._rfc_batch) rather
than one-shot calls -- the batch runs the whole sequence inside a single
JCoContext, and the bridge logs off again even if a call in the middle fails.
"""

import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger("syntaai.tools.basis.os_monitor")

_MONITOR = "Operating System"
_MONITOR_SETS = ("SAP CCMS Monitor Templates", "SAP CCMS Technical Expert Monitors")

# BAPITID: the fields that identify one monitoring tree element.
_TID_FIELDS = ("MTSYSID", "MTMCNAME", "MTNUMRANGE", "MTUID", "MTCLASS", "MTINDEX", "EXTINDEX")

_PERF_CLASS = "100"          # CCMS class 100 = performance attribute

# CCMS alert status on a performance value.
_ALERT_COLOURS = {"0": "inactive", "1": "green", "2": "yellow", "3": "red"}
_BAD_COLOURS = {"yellow", "red"}

# (OBJECTNAME, MTNAMESHRT) -> key in the per-host summary. Names verified against
# the live tree; anything not listed here is skipped so the batch stays small.
_WANTED = {
    ("CPU", "Utilization"): "cpu_pct",
    ("CPU", "User Utilization"): "cpu_user_pct",
    ("CPU", "System Utilization"): "cpu_system_pct",
    ("CPU", "Idle"): "cpu_idle_pct",
    ("CPU", "5minLoadAverage"): "load_5min",
    # CCMS exposes BOTH a percentage ("Free") and an absolute ("Free (Value)")
    # under Memory. Mapping "Free" to a megabyte field reads as 7 MB free on a
    # host with 80 GB, which is alarming and wrong -- it is 7 percent.
    ("Memory", "Free"): "mem_free_pct",
    ("Memory", "Free (Value)"): "mem_free_mb",
    ("Memory", "Free Memory"): "mem_free_alt",
    ("Memory", "Physical"): "mem_physical_mb",
    ("Paging", "Page_In"): "paging_in",
    ("Paging", "Page_Out"): "paging_out",
    ("Swap_Space", "Percentage_Used"): "swap_pct",
    ("Swap_Space", "Freespace"): "swap_free",
    ("eth0", "Errors In"): "lan_errors_in",
    ("eth0", "Errors Out"): "lan_errors_out",
}

_FS_PCT = "Percentage_Used"
_FS_FREE = "Freespace"
_FS_ALERT_PCT = 85.0
_IDLE_FLOOR_PCT = 10.0
_MAX_DETAIL = 20

_UNITS_NOTE = (
    "Values are reported exactly as CCMS/saposcol supplies them. Percentages are "
    "percent; memory, swap and filesystem sizes follow saposcol's own units "
    "(normally MB for memory, KB for filesystem free space) and are not converted."
)


def _clean(v):
    return ("" if v is None else str(v)).strip()


def _num(v):
    try:
        return float(_clean(v))
    except (TypeError, ValueError):
        return None


def register_os_monitor_tools(mcp, connector):
    """Register operating-system monitoring tools on the given FastMCP instance."""

    def _logon(user):
        return {"function": "BAPI_XMI_LOGON",
                "params": {"EXTCOMPANY": "SyntaAI", "EXTPRODUCT": "MCP",
                           "INTERFACE": "XAL", "VERSION": "1.0"}}

    async def _batch(calls):
        """Run a stateful batch, returning (payload, error_or_None). Never raises."""
        if not hasattr(connector, "_rfc_batch"):
            return None, "This connector has no /api/rfc/batch support (JCo bridge required)."
        try:
            res = await connector._rfc_batch(calls)
        except Exception as exc:
            logger.warning("rfc batch failed: %s", exc)
            return None, str(exc)
        if not res.get("success"):
            return res, res.get("error") or "batch reported failure"
        return res, None

    # ------------------------------------------------------------------
    # 1. get_os_resource_snapshot
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_os_resource_snapshot(
        external_user_name: str = "SYNTAAI_MCP",
        max_metrics: int = 60,
    ) -> dict:
        """Show host CPU, memory, paging, swap and filesystem use (ST06 / OS07N) from CCMS.

        Collected over RFC from CCMS, which reads saposcol. No SSH or shell access is
        used. Depends on saposcol running and the CCMS monitoring architecture being
        active; CCMS is deprecated but still shipped in S/4HANA.

        Args:
            external_user_name: Name registered with the CCMS XAL interface for this session.
            max_metrics: Cap on individual MTE value reads in one batch (default 60).
        """
        now = datetime.utcnow()
        base = {
            "source": "live_sap",
            "collector": "CCMS/saposcol via RFC",
            "limitations": "Depends on saposcol running and the CCMS monitoring architecture "
                           "being active. CCMS is deprecated but still present in S/4HANA. "
                           + _UNITS_NOTE,
            "checked_at": now.isoformat() + "Z",
        }
        try:
            max_metrics = min(max(int(max_metrics), 1), 200)

            # --- 1. monitor tree, trying each candidate monitor set in turn ---
            nodes, used_set, last_msg = [], None, ""
            for mset in _MONITOR_SETS:
                res, err = await _batch([
                    _logon(external_user_name),
                    {"function": "BAPI_SYSTEM_MON_GETTREE",
                     "params": {"EXTERNAL_USER_NAME": external_user_name,
                                "MONITOR_NAME": {"MS_NAME": mset, "MONI_NAME": _MONITOR}}},
                ])
                if err and res is None:
                    return {**base, "status": "os_layer_unavailable", "reason": err,
                            "hosts": [], "flagged": []}
                results = (res or {}).get("results", [])
                if len(results) < 2:
                    last_msg = err or "monitor tree call did not run"
                    continue
                tree = results[1]
                export = tree.get("export", {})
                found = tree.get("tables", {}).get("TREE_NODES", [])
                if found:
                    nodes, used_set = found, mset
                    break
                last_msg = _clean(export.get("MESSAGE")) or last_msg

            if not nodes:
                return {
                    **base, "status": "os_layer_unavailable",
                    "reason": f"CCMS returned no '{_MONITOR}' tree from any of "
                              f"{list(_MONITOR_SETS)}. SAP said: {last_msg or '(no message)'}",
                    "hosts": [], "flagged": [],
                }

            # --- 2. pick the performance MTEs worth reading ---
            wanted = []
            for n in nodes:
                if _clean(n.get("MTCLASS")) != _PERF_CLASS:
                    continue
                obj, short = _clean(n.get("OBJECTNAME")), _clean(n.get("MTNAMESHRT"))
                if (obj, short) in _WANTED:
                    wanted.append((n, _WANTED[(obj, short)], None))
                elif obj.startswith("/") and short in (_FS_PCT, _FS_FREE):
                    wanted.append((n, short, obj))
            truncated = len(wanted) > max_metrics
            wanted = wanted[:max_metrics]

            if not wanted:
                return {**base, "status": "os_layer_unavailable",
                        "reason": "The CCMS tree contained no recognised performance MTEs.",
                        "monitor_set": used_set, "hosts": [], "flagged": []}

            # --- 3. read every current value in ONE stateful batch ---
            calls = [_logon(external_user_name)]
            for n, _key, _mount in wanted:
                calls.append({"function": "BAPI_SYSTEM_MTE_GETPERFCURVAL",
                              "params": {"EXTERNAL_USER_NAME": external_user_name,
                                         "TID": {f: n.get(f, "") for f in _TID_FIELDS}}})
            res, err = await _batch(calls)
            if res is None:
                return {**base, "status": "os_layer_unavailable", "reason": err,
                        "monitor_set": used_set, "hosts": [], "flagged": []}
            values = res.get("results", [])[1:]

            # --- 4. fold into per-host summaries ---
            hosts = defaultdict(lambda: {"filesystems": {}})
            flagged = []
            for (node, key, mount), value in zip(wanted, values):
                cur = (value or {}).get("export", {}).get("CURRENT_VALUE", {}) or {}
                raw = cur.get("ALRELEVVAL", cur.get("LASTPERVAL"))
                num = _num(raw)
                colour = _ALERT_COLOURS.get(_clean(cur.get("LASTALSTAT")), "unknown")
                host = _clean(node.get("MTMCNAME")) or "(unknown host)"
                entry = hosts[host]
                entry["host"] = host
                entry.setdefault("system_id", _clean(node.get("MTSYSID")))
                entry.setdefault("measured_at",
                                 f"{_clean(cur.get('ALRELVALDT'))} {_clean(cur.get('ALRELVALTI'))}".strip())

                if mount:
                    fs = entry["filesystems"].setdefault(mount, {"mount": mount})
                    if key == _FS_PCT:
                        fs["pct_used"] = num
                        fs["alert"] = colour
                        if num is not None and num > _FS_ALERT_PCT:
                            flagged.append({"host": host, "metric": f"filesystem {mount}",
                                            "value": num, "unit": "%", "alert": colour,
                                            "why": f"above {_FS_ALERT_PCT:.0f}% used"})
                    else:
                        fs["free"] = num
                else:
                    entry[key] = num
                    entry.setdefault("alerts", {})[key] = colour

                if colour in _BAD_COLOURS:
                    flagged.append({"host": host, "metric": mount or key, "value": num,
                                    "alert": colour, "why": f"CCMS alert is {colour}"})

            out_hosts = []
            for host, entry in hosts.items():
                entry["filesystems"] = sorted(entry["filesystems"].values(),
                                              key=lambda f: (f.get("pct_used") or -1),
                                              reverse=True)[:_MAX_DETAIL]
                idle = entry.get("cpu_idle_pct")
                if idle is not None and idle < _IDLE_FLOOR_PCT:
                    flagged.append({"host": host, "metric": "cpu_idle_pct", "value": idle,
                                    "unit": "%", "alert": "red",
                                    "why": f"CPU idle below {_IDLE_FLOOR_PCT:.0f}%"})
                out_hosts.append(entry)

            # de-duplicate flags on (host, metric), keeping the first reason
            seen, unique = set(), []
            for f in flagged:
                k = (f["host"], f["metric"])
                if k in seen:
                    continue
                seen.add(k)
                unique.append(f)

            return {
                **base,
                "status": "ok",
                "monitor_set": used_set,
                "monitor": _MONITOR,
                "metrics_read": len(wanted),
                "metrics_truncated": truncated,
                "hosts": out_hosts[:_MAX_DETAIL],
                "flagged_count": len(unique),
                "flagged": unique[:_MAX_DETAIL],
                "risk": "HIGH" if any(f["alert"] == "red" for f in unique)
                        else ("MEDIUM" if unique else "LOW"),
            }
        except Exception as e:
            return {**base, "status": "os_layer_unavailable", "reason": str(e),
                    "hosts": [], "flagged": []}
