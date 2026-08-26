"""Three Basis workflows, registered via register_prompts().

Each one is a starting instruction for the model, not a script: it names the
tools worth calling and the order that answers the question fastest.
"""


def register_prompts(mcp):
    """Register the Basis prompts on the given FastMCP instance."""

    @mcp.prompt()
    def system_health_check() -> str:
        """Check whether the system is healthy right now, and say what to look at first."""
        return (
            "Establish the current health of this SAP system and report it the way a Basis "
            "administrator would to a colleague taking over a shift.\n\n"
            "Start with get_system_health_summary for the overall picture, then get_instance_status "
            "to confirm every application server is up. Read get_syslog_critical for the last few "
            "hours and check_update_errors for failed updates (SM13). Look at "
            "get_work_process_status for processes stuck or in PRIV mode, and "
            "get_os_resource_snapshot for CPU, memory and filesystem pressure on the host.\n\n"
            "Report: one sentence on whether the system is healthy. Then anything that needs "
            "attention today, most urgent first, each with the transaction an administrator would "
            "open to act on it. State plainly when a check returned nothing rather than implying "
            "it passed. Do not speculate about causes the data does not support."
        )

    @mcp.prompt()
    def dump_investigation() -> str:
        """Investigate ABAP short dumps: what is failing, how often, and whether it is new."""
        return (
            "Investigate the ABAP short dumps (ST22) on this system.\n\n"
            "Begin with get_dump_summary for totals and the top errors, programs and users. Use "
            "get_repeat_dumps to separate a recurring fault from one-off noise, and "
            "get_dump_analysis for pattern detection. If one program dominates, follow up with "
            "get_dumps_by_program for its trend. Cross-check check_update_errors: a failing update "
            "module often shows up in both.\n\n"
            "Report: which dumps are recurring and which are isolated, how far back the pattern "
            "goes, and which user or program is most affected. Distinguish an application error "
            "from a system-level one. If the evidence does not identify a root cause, say so and "
            "name what an administrator would need to look at in ST22 to find it."
        )

    @mcp.prompt()
    def queue_and_lock_triage() -> str:
        """Triage stuck RFC queues and long-held locks — the usual cause of a hung interface."""
        return (
            "Triage the RFC and locking layer, which is where a hung interface usually shows up.\n\n"
            "Call get_trfc_queue_status for transactional RFC (SM58) and get_qrfc_queue_status for "
            "the inbound and outbound queues (SMQ1/SMQ2). Then get_lock_entries for locks held "
            "longer than a few hours.\n\n"
            "Treat the three states differently. SYSFAIL and CPICERR are failures and need a named "
            "destination and error text. STOP on a qRFC queue is normally deliberate — somebody "
            "locked it in SMQ1 — so report it as held, not broken. A long-held lock is usually a "
            "hung dialog session or a cancelled batch job, and deleting it in SM12 can corrupt the "
            "document the holder was editing; identify the user, host and work process and say the "
            "session must be confirmed dead first.\n\n"
            "Report: what is stuck, since when, and the single next action for each item."
        )
