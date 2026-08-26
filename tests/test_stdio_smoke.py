"""Start the server in stdio mode and confirm it lists exactly 25 tools.

Speaks MCP over the child process's stdin/stdout, the same way Claude Desktop
does. No network, no SAP calls -- tools/list does not touch the backend.

    cd sap-basis-copilot && venv/bin/python tests/test_stdio_smoke.py
"""

import json
import os
import subprocess
import sys

EXPECT = 25
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _frame(obj):
    return (json.dumps(obj) + "\n").encode()


def _read_result(pipe, want_id):
    """Read newline-delimited JSON-RPC lines until the reply with want_id arrives."""
    while True:
        line = pipe.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode())
        except ValueError:
            continue
        if msg.get("id") == want_id:
            return msg


def main():
    env = dict(os.environ, MCP_TRANSPORT="stdio", LOG_LEVEL="WARNING")
    proc = subprocess.Popen(
        [sys.executable, "server.py"], cwd=ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(_frame({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "stdio-smoke", "version": "1"}},
        }))
        proc.stdin.flush()
        _read_result(proc.stdout, 1)  # initialize result
        proc.stdin.write(_frame({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        proc.stdin.flush()
        proc.stdin.write(_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        proc.stdin.flush()
        msg = _read_result(proc.stdout, 2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    tools = (msg or {}).get("result", {}).get("tools", [])
    names = sorted(t["name"] for t in tools)
    print(f"stdio server listed {len(tools)} tools")
    for n in names:
        print("  -", n)
    if len(tools) != EXPECT:
        print(f"FAIL: expected {EXPECT}, got {len(tools)}")
        return 1
    print(f"PASS: {EXPECT} tools over stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
