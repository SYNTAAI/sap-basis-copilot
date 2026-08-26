"""JCo REST bridge connector — the only SAP transport this server uses.

Every call goes to the local bridge (bridge/SapJcoRest.java), which refuses any
function module outside its own allow-list with HTTP 403 before it opens a
connection to SAP. The list below mirrors the bridge's default.
"""

import logging
from typing import Dict, List, Optional

import httpx

from .base import BaseSAPConnector

logger = logging.getLogger("basis_copilot.jco")

# The complete set of function modules this server is permitted to call.
# All nine are read-only. The bridge enforces the same list; this copy exists so
# the constraint is visible from the Python side too.
ALLOWED_RFC_FUNCTIONS = (
    "RFC_READ_TABLE",                 # generic table read
    "RFC_SYSTEM_INFO",                # instance and system identity
    "TH_WPINFO",                      # work process list
    "ENQUEUE_READ",                   # SM12 lock entries
    "BAPI_XMI_LOGON",                 # opens the stateful CCMS/XAL session
    "BAPI_XMI_LOGOFF",                # closes it
    "BAPI_SYSTEM_MS_GETLIST",         # CCMS monitor sets
    "BAPI_SYSTEM_MON_GETTREE",        # CCMS monitor tree
    "BAPI_SYSTEM_MTE_GETPERFCURVAL",  # CCMS current MTE value
    "BAPI_SYSTEM_MTE_GETMLHIS",        # CCMS message-log history (SM21 syslog)
)


class JCoConnector(BaseSAPConnector):
    """Calls the local JCo REST bridge. Read-only by construction."""

    def __init__(self, jco_url: str = "http://127.0.0.1:8080", ashost: str = "",
                 sysnr: str = "00", client: str = "100", user: str = "",
                 passwd: str = "", lang: str = "EN", timeout: float = 60.0):
        self.jco_url = jco_url.rstrip("/")
        self.connection = {"ashost": ashost, "sysnr": sysnr, "client": client,
                           "user": user, "passwd": passwd, "lang": lang}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def _rfc_call(self, function: str, parameters: Optional[Dict] = None) -> Dict:
        """Call one function module. Returns {"export": {...}, "tables": {...}}."""
        payload = {"connection": self.connection, "function": function,
                   "parameters": parameters or {}}
        logger.info("RFC call: %s", function)
        resp = await self._client.post(f"{self.jco_url}/api/rfc/call", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def _rfc_batch(self, calls: List[Dict]) -> Dict:
        """Run a sequence in ONE stateful context (needed for CCMS/XAL).

        The bridge wraps the whole sequence in JCoContext.begin/end and logs the
        XMI session off again even if a call in the middle fails.
        """
        payload = {"connection": self.connection, "calls": calls}
        logger.info("RFC batch: %s", [c.get("function") for c in calls])
        resp = await self._client.post(f"{self.jco_url}/api/rfc/batch", json=payload)
        if resp.status_code >= 400:
            try:
                return resp.json()          # 403 allow-list / 500 sequence failure
            except Exception:
                resp.raise_for_status()
        return resp.json()

    async def _table_read(self, table: str, fields: Optional[List[str]] = None,
                          where: str = "", max_rows: int = 1000) -> List[Dict]:
        """Read an SAP table through RFC_READ_TABLE."""
        payload = {"connection": self.connection, "table": table,
                   "fields": fields or [], "where": where, "maxRows": max_rows}
        logger.info("table read: %s where=%s", table, where)
        resp = await self._client.post(f"{self.jco_url}/api/table/read", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("rows", data.get("results", []))

    async def get_system_info(self) -> Dict:
        return await self._rfc_call("RFC_SYSTEM_INFO")

    async def health(self) -> Dict:
        resp = await self._client.get(f"{self.jco_url}/api/health")
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()
