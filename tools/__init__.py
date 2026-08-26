"""Tool registration entry point for SAP Basis Copilot."""

from .basis import (
    register_dump_tools,
    register_lock_tools,
    register_monitoring_tools,
    register_os_monitor_tools,
    register_performance_tools,
    register_queue_tools,
    register_spool_tools,
    register_system_tools,
    register_task_list_tools,
)


def register_all_tools(mcp, connector):
    """Register all 25 read-only Basis tools on the given FastMCP instance."""
    # System monitoring (4): health summary, SM51 instances, SM21 syslog, SM13 updates
    register_monitoring_tools(mcp, connector)
    # Work processes and response times (6): SM50/SM66, ST03N, ST05/DB02, ST02, RZ10/RZ11
    register_performance_tools(mcp, connector)
    # System administration (3): SM37 background jobs, T000 client settings, SNC
    register_system_tools(mcp, connector)
    # ABAP dumps (4): ST22 summary, repeats, by program, analysis
    register_dump_tools(mcp, connector)
    # Task lists (3): STC01 browser, detail view, STC02 run history
    register_task_list_tools(mcp, connector)
    # Lock entries (1): SM12 via ENQUEUE_READ
    register_lock_tools(mcp, connector)
    # RFC queues (2): SM58 tRFC, SMQ1/SMQ2 qRFC
    register_queue_tools(mcp, connector)
    # Spool (1): SP01/SP02 spool and output requests
    register_spool_tools(mcp, connector)
    # Operating system (1): ST06/OS07N host resources via CCMS
    register_os_monitor_tools(mcp, connector)
