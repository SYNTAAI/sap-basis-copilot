"""Live smoke tests for the five new Basis tools (SM12, SM58, SMQ1/2, SP01, ST06).

Runs against the SAP system configured in .env, exactly as server.py does:
Settings() -> ConnectorFactory.create(). Read-only throughout.

    cd /opt/mcp-server-live && venv/bin/python tests/test_basis_new.py
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Settings                                     # noqa: E402
from connectors import ConnectorFactory                        # noqa: E402
from tools.basis.locks import register_lock_tools               # noqa: E402
from tools.basis.os_monitor import register_os_monitor_tools    # noqa: E402
from tools.basis.queues import register_queue_tools             # noqa: E402
from tools.basis.spool import register_spool_tools              # noqa: E402

TIME_LIMIT_S = 30.0
MAX_DETAIL = 20


class _Collector:
    """Stands in for FastMCP: captures the functions the register_* helpers decorate."""

    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _detail_lists(node, path="", found=None):
    """Every list-of-dicts in the response with its path, for the <=20 cap check."""
    found = found if found is not None else []
    if isinstance(node, dict):
        for k, v in node.items():
            _detail_lists(v, f"{path}.{k}" if path else k, found)
    elif isinstance(node, list):
        if node and isinstance(node[0], dict):
            found.append((path, len(node)))
        for i, v in enumerate(node):
            _detail_lists(v, f"{path}[{i}]", found)
    return found


CASES = [
    ("get_lock_entries",         {}, ["total_locks", "by_mode", "by_age", "top_users", "stale_locks", "risk"]),
    ("get_trfc_queue_status",    {}, ["total_entries", "by_state", "top_destinations", "flagged", "risk"]),
    ("get_qrfc_queue_status",    {}, ["directions", "failed_queue_count", "risk"]),
    ("get_spool_status",         {}, ["spool_requests", "output_requests", "risk"]),
    ("get_os_resource_snapshot", {}, ["status", "collector", "limitations", "hosts", "flagged"]),
]


async def main():
    settings = Settings()
    connector = ConnectorFactory.create(settings)

    mcp = _Collector()
    for reg in (register_lock_tools, register_queue_tools,
                register_spool_tools, register_os_monitor_tools):
        reg(mcp, connector)

    print(f"registered {len(mcp.tools)} new tools: {', '.join(sorted(mcp.tools))}\n")
    failures = []

    for name, kwargs, required in CASES:
        if name not in mcp.tools:
            failures.append(f"{name}: not registered")
            continue
        started = time.monotonic()
        try:
            resp = await mcp.tools[name](**kwargs)
        except Exception as exc:
            failures.append(f"{name}: raised {exc!r}")
            print(f"### {name}\n  RAISED: {exc!r}\n")
            continue
        elapsed = time.monotonic() - started

        if resp.get("source") != "live_sap":
            failures.append(f"{name}: source={resp.get('source')!r}, expected 'live_sap'")
        missing = [k for k in required if k not in resp]
        if missing:
            failures.append(f"{name}: missing summary keys {missing}")
        oversized = [(p, n) for p, n in _detail_lists(resp) if n > MAX_DETAIL]
        if oversized:
            failures.append(f"{name}: detail list(s) over {MAX_DETAIL}: {oversized}")
        if elapsed >= TIME_LIMIT_S:
            failures.append(f"{name}: took {elapsed:.1f}s (limit {TIME_LIMIT_S}s)")

        print(f"### {name}   [{elapsed:.2f}s]")
        printable = {k: v for k, v in resp.items()
                     if not (isinstance(v, list) and len(v) > 3)}
        print(json.dumps(printable, indent=2, default=str)[:2400])
        print()

    await connector.close()

    print("=" * 62)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED - 5 tools, source=live_sap, keys present, "
          f"no detail list over {MAX_DETAIL}, all under {TIME_LIMIT_S:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
