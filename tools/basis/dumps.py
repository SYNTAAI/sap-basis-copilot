"""ABAP dump analysis tools – 4 tools registered via register_dump_tools().

SNAP table structure (SAP kernel):
  Key columns: MANDT, DATUM, UZEIT, AHOST, UNAME, MODNO, SEQNO
  SEQNO '000' = header row containing packed FLIST with error type + program
  FLIST encoding: two-char prefix + 3-digit length + value
    FC = error type (RUNTIME_ERR), AP = program (REPID)
"""

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from utils.date_helpers import parse_sap_date, fmt_date
from utils.risk_helpers import risk_level

logger = logging.getLogger("syntaai.tools.basis.dumps")

# Map SNAP error types to likely root causes
_ERROR_CAUSE_MAP = {
    "RABAX_STATE": "Unhandled exception in ABAP code — needs developer fix",
    "TIMEOUT": "Program running too long — check DB query or loop logic",
    "STORAGE_NO_ROLL": "Roll area memory exhausted — increase roll area or fix program",
    "TSV_TNEW_PAGE_ALLOC_FAILED": "Memory allocation failed — system memory pressure",
    "LOAD_PROGRAM_NOT_FOUND": "Missing program or transport issue",
    "CALL_FUNCTION_REMOTE_ERROR": "RFC connection failure",
    "CALL_FUNCTION_ACCEPT_FAILED": "RFC destination unavailable or connection rejected",
    "CALL_FUNCTION_SEND_ERROR": "RFC communication error during data transfer",
    "MESSAGE_TYPE_X": "Intentional program termination — check program logic",
    "ASSERTION_FAILED": "ABAP assertion violated — programming error in code logic",
    "OBJECTS_OBJREF_NOT_ASSIGNED": "Null object reference — program tried to use uninitialized object",
    "ASSIGN_TYPE_CONFLICT": "Field symbol type mismatch — data type incompatibility",
    "SYNTAX_ERROR": "ABAP syntax error at runtime — likely missing transport or include",
    "DDIC_TYPELENG_INCONSISTENT": "Data dictionary inconsistency — activate DDIC objects or run SPDD",
    "DYNPRO_SYNTAX_ERROR": "Dynpro (screen) generation error — regenerate screen via SE80",
    "SAPSQL_DATA_LOSS": "Data truncation in Open SQL — field length mismatch",
}

# Actual SNAP table columns we need (SEQNO=000 for header rows)
_SNAP_FIELDS = ["DATUM", "UZEIT", "AHOST", "UNAME", "MODNO", "FLIST"]


def _likely_cause(error_type: str) -> str:
    return _ERROR_CAUSE_MAP.get(error_type, f"Unknown error type '{error_type}' — review ST22 for details")


def _date_filter(days: int) -> str:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    return f"SEQNO = '000' AND DATUM >= '{cutoff}'"


def _parse_flist_field(flist: str, prefix: str) -> str:
    """Extract a value from SNAP FLIST packed format.

    Format: <2-char prefix><3-digit length><value>
    Example: FC016ASSERTION_FAILED → prefix='FC', length=16, value='ASSERTION_FAILED'
    """
    idx = flist.find(prefix)
    if idx < 0:
        return ""
    try:
        length = int(flist[idx + 2:idx + 5])
        raw = flist[idx + 5:idx + 5 + length]
        return raw.rstrip("=").strip()
    except (ValueError, IndexError):
        return ""


def _parse_snap_row(row: dict) -> dict:
    """Parse a raw SNAP SEQNO=000 row into a normalized dump record."""
    flist = row.get("FLIST", "")
    return {
        "DATUM": row.get("DATUM", ""),
        "UZEIT": row.get("UZEIT", ""),
        "UNAME": row.get("UNAME", ""),
        "AHOST": row.get("AHOST", ""),
        "MODNO": row.get("MODNO", ""),
        "RUNTIME_ERR": _parse_flist_field(flist, "FC"),
        "REPID": _parse_flist_field(flist, "AP"),
    }


def _parse_datum(datum: str) -> str:
    """Format YYYYMMDD → YYYY-MM-DD."""
    dt = parse_sap_date(datum)
    return fmt_date(datum) if dt else str(datum)


def _parse_time(uzeit: str) -> str:
    """Format HHMMSS → HH:MM:SS."""
    t = str(uzeit).strip()
    if len(t) == 6 and t.isdigit():
        return f"{t[:2]}:{t[2:4]}:{t[4:6]}"
    return t


def _compute_trend(records: list, days: int) -> str:
    """Compare dump count in first half vs second half of period."""
    midpoint = datetime.now() - timedelta(days=days / 2)
    first_half = 0
    second_half = 0
    for r in records:
        dt = parse_sap_date(r.get("DATUM", ""))
        if dt is None:
            continue
        if dt < midpoint:
            first_half += 1
        else:
            second_half += 1
    if second_half > first_half * 1.2:
        return "INCREASING"
    if first_half > second_half * 1.2:
        return "DECREASING"
    return "STABLE"


def _overall_risk(total: int, repeat_count: int, last_24h_max: int) -> str:
    """Compute overall risk from dump metrics."""
    if last_24h_max > 10:
        return "CRITICAL"
    if repeat_count > 0 or total > 50:
        return "HIGH"
    if total >= 10:
        return "MEDIUM"
    return "LOW"


def _last_24h_max_by_error(records: list) -> int:
    """Max count of any single error type in the last 24 hours."""
    cutoff = datetime.now() - timedelta(hours=24)
    counts: Counter = Counter()
    for r in records:
        dt = parse_sap_date(r.get("DATUM", ""))
        if dt and dt >= cutoff:
            counts[r.get("RUNTIME_ERR", "")] += 1
    return max(counts.values()) if counts else 0


async def _fetch_snap(connector, days: int) -> list:
    """Fetch SNAP header rows (SEQNO=000) and parse FLIST into normalized records."""
    if not hasattr(connector, "_table_read"):
        return []
    try:
        raw_rows = await connector._table_read(
            "SNAP",
            fields=_SNAP_FIELDS,
            where=_date_filter(days),
            max_rows=10000,
        )
    except Exception as exc:
        logger.warning("SNAP table read failed: %s", exc)
        return []
    return [_parse_snap_row(r) for r in raw_rows]


def register_dump_tools(mcp, connector):
    """Register all ABAP dump analysis tools on the given FastMCP instance."""

    # ------------------------------------------------------------------
    # 1. get_dump_summary
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_dump_summary(days: int = 7) -> dict:
        """Aggregated ABAP dump (ST22) summary: totals, top errors, top programs, top users, risk assessment."""
        rows = await _fetch_snap(connector, days)
        if not rows:
            return {
                "source": "live_sap",
                "period_days": days,
                "total_dumps": 0,
                "note": "No dumps found or SNAP table not accessible via current connector",
                "overall_risk": "LOW",
                "recommendation": "No dumps detected. System appears healthy.",
            }

        total = len(rows)
        error_counts: Counter = Counter()
        program_counts: Counter = Counter()
        user_counts: Counter = Counter()
        error_dates: defaultdict = defaultdict(list)
        program_errors: defaultdict = defaultdict(set)

        for r in rows:
            err = r.get("RUNTIME_ERR") or "UNKNOWN"
            prog = r.get("REPID") or "UNKNOWN"
            user = r.get("UNAME") or "UNKNOWN"
            datum = r.get("DATUM", "")

            error_counts[err] += 1
            program_counts[prog] += 1
            user_counts[user] += 1
            error_dates[err].append(datum)
            program_errors[prog].add(err)

        repeat_count = sum(1 for c in error_counts.values() if c > 3)
        last_24h_max = _last_24h_max_by_error(rows)
        risk = _overall_risk(total, repeat_count, last_24h_max)

        # Top errors
        top_errors = []
        for err, count in error_counts.most_common(10):
            dates_sorted = sorted(error_dates[err])
            err_rows = [r for r in rows if r.get("RUNTIME_ERR") == err]
            top_errors.append({
                "error_type": err,
                "count": count,
                "first_seen": _parse_datum(dates_sorted[0]) if dates_sorted else "N/A",
                "last_seen": _parse_datum(dates_sorted[-1]) if dates_sorted else "N/A",
                "trend": _compute_trend(err_rows, days),
                "risk": "CRITICAL" if count > 10 and last_24h_max > 10 else risk_level(count, high_threshold=5, medium_threshold=2),
            })

        # Top programs
        top_programs = []
        for prog, count in program_counts.most_common(10):
            top_programs.append({
                "program": prog,
                "dump_count": count,
                "error_types": sorted(program_errors[prog]),
                "risk": risk_level(count, high_threshold=5, medium_threshold=2),
            })

        # Top users
        top_users = []
        for user, count in user_counts.most_common(10):
            user_errs = set()
            for r in rows:
                if r.get("UNAME") == user:
                    user_errs.add(r.get("RUNTIME_ERR", ""))
            top_users.append({
                "user": user,
                "dump_count": count,
                "error_types": sorted(user_errs),
            })

        # Recommendation
        if risk == "CRITICAL":
            rec = f"CRITICAL: {top_errors[0]['error_type']} has >10 dumps in the last 24 hours. Investigate immediately in ST22."
        elif risk == "HIGH":
            rec = f"HIGH: {repeat_count} error type(s) are repeating (>3 occurrences). Review top errors in ST22 and engage developers."
        elif risk == "MEDIUM":
            rec = f"MEDIUM: {total} dumps in {days} days. Monitor trends and review top programs for optimization."
        else:
            rec = f"LOW: Only {total} dump(s) in {days} days. System is healthy."

        return {
            "source": "live_sap",
            "period_days": days,
            "total_dumps": total,
            "unique_error_types": len(error_counts),
            "unique_programs": len(program_counts),
            "unique_users": len(user_counts),
            "repeat_dumps": repeat_count,
            "overall_risk": risk,
            "top_errors": top_errors,
            "top_programs": top_programs,
            "top_users": top_users,
            "recommendation": rec,
        }

    # ------------------------------------------------------------------
    # 2. get_repeat_dumps
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_repeat_dumps(days: int = 7, min_occurrences: int = 3) -> dict:
        """Find recurring ABAP dumps: same error+program combinations occurring multiple times, with root cause mapping."""
        rows = await _fetch_snap(connector, days)
        if not rows:
            return {
                "source": "live_sap",
                "repeat_patterns": [],
                "total_patterns": 0,
                "overall_risk": "LOW",
                "recommendation": "No dumps found or SNAP table not accessible.",
            }

        # Group by (error_type, program)
        pattern_groups: defaultdict = defaultdict(list)
        for r in rows:
            key = (r.get("RUNTIME_ERR") or "UNKNOWN", r.get("REPID") or "UNKNOWN")
            pattern_groups[key].append(r)

        patterns = []
        for (err, prog), recs in pattern_groups.items():
            if len(recs) < min_occurrences:
                continue
            dates = sorted(r.get("DATUM", "") for r in recs)
            users = sorted(set(r.get("UNAME", "") for r in recs))
            first_dt = parse_sap_date(dates[0])
            last_dt = parse_sap_date(dates[-1])
            days_active = (last_dt - first_dt).days + 1 if first_dt and last_dt else 0

            patterns.append({
                "error_type": err,
                "program": prog,
                "occurrences": len(recs),
                "affected_users": users,
                "first_seen": _parse_datum(dates[0]),
                "last_seen": _parse_datum(dates[-1]),
                "days_active": days_active,
                "risk": risk_level(len(recs), high_threshold=5, medium_threshold=3),
                "likely_cause": _likely_cause(err),
                "recommended_action": f"Review program {prog} in ST22 for error {err}. {_likely_cause(err)}",
            })

        patterns.sort(key=lambda p: p["occurrences"], reverse=True)
        last_24h_max = _last_24h_max_by_error(rows)
        risk = _overall_risk(len(rows), len(patterns), last_24h_max)

        if patterns:
            rec = f"{len(patterns)} recurring pattern(s) detected. Top issue: {patterns[0]['error_type']} in {patterns[0]['program']} ({patterns[0]['occurrences']} times). Fix the most frequent patterns first."
        else:
            rec = f"No recurring patterns found (threshold: {min_occurrences}+ occurrences). Dumps appear to be isolated incidents."

        return {
            "source": "live_sap",
            "repeat_patterns": patterns,
            "total_patterns": len(patterns),
            "overall_risk": risk,
            "recommendation": rec,
        }

    # ------------------------------------------------------------------
    # 3. get_dumps_by_program
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_dumps_by_program(program: str, days: int = 30) -> dict:
        """Get all ABAP dumps for a specific program (partial match), with trend analysis and error breakdown."""
        rows = await _fetch_snap(connector, days)
        if not rows:
            return {
                "source": "live_sap",
                "program": program,
                "total_dumps": 0,
                "dumps": [],
                "overall_risk": "LOW",
                "recommendation": "No dumps found or SNAP table not accessible.",
            }

        # Filter by program (case-insensitive partial match)
        prog_upper = program.upper()
        filtered = [r for r in rows if prog_upper in (r.get("REPID") or "").upper()]

        if not filtered:
            return {
                "source": "live_sap",
                "program": program,
                "total_dumps": 0,
                "dumps": [],
                "overall_risk": "LOW",
                "recommendation": f"No dumps found for program matching '{program}' in the last {days} days.",
            }

        # Build dump list
        dumps = []
        error_breakdown: Counter = Counter()
        affected_users: set = set()
        for r in filtered:
            err = r.get("RUNTIME_ERR") or "UNKNOWN"
            error_breakdown[err] += 1
            affected_users.add(r.get("UNAME") or "UNKNOWN")
            d_since = None
            dt = parse_sap_date(r.get("DATUM", ""))
            if dt:
                d_since = (datetime.now() - dt).days
            dumps.append({
                "dump_id": f"{r.get('DATUM', '')}-{r.get('UZEIT', '')}-{r.get('MODNO', '')}",
                "date": _parse_datum(r.get("DATUM", "")),
                "time": _parse_time(r.get("UZEIT", "")),
                "error_type": err,
                "user": r.get("UNAME", ""),
                "likely_cause": _likely_cause(err),
                "days_ago": d_since,
            })

        dumps.sort(key=lambda d: (d["date"], d["time"]), reverse=True)
        trend = _compute_trend(filtered, days)
        total = len(filtered)
        repeat_count = sum(1 for c in error_breakdown.values() if c > 3)
        last_24h_max = _last_24h_max_by_error(filtered)
        risk = _overall_risk(total, repeat_count, last_24h_max)

        rec = f"Program '{program}': {total} dump(s) in {days} days. Trend: {trend}. "
        if trend == "INCREASING":
            rec += "Dumps are increasing — investigate recent changes to this program."
        elif repeat_count > 0:
            top_err = error_breakdown.most_common(1)[0][0]
            rec += f"Recurring error '{top_err}' — {_likely_cause(top_err)}"
        else:
            rec += "Review error types in ST22 for root cause details."

        return {
            "source": "live_sap",
            "program": program,
            "total_dumps": total,
            "dumps": dumps,
            "error_type_breakdown": dict(error_breakdown),
            "affected_users": sorted(affected_users),
            "trend": trend,
            "overall_risk": risk,
            "recommendation": rec,
        }

    # ------------------------------------------------------------------
    # 4. get_dump_analysis
    # ------------------------------------------------------------------
    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def get_dump_analysis(days: int = 1) -> dict:
        """Intelligent ABAP dump analysis: pattern detection, root cause hypothesis, and action plan."""
        rows = await _fetch_snap(connector, days)
        if not rows:
            return {
                "source": "live_sap",
                "analysis_period_hours": days * 24,
                "total_dumps": 0,
                "health_status": "HEALTHY",
                "primary_issue": None,
                "secondary_issues": [],
                "patterns_detected": [],
                "root_cause_hypothesis": "No dumps detected — system appears healthy.",
                "action_plan": [],
                "overall_risk": "LOW",
                "recommendation": "No dumps found. System is running normally.",
            }

        total = len(rows)
        error_counts: Counter = Counter()
        program_counts: Counter = Counter()
        user_counts: Counter = Counter()
        error_programs: defaultdict = defaultdict(set)
        error_users: defaultdict = defaultdict(set)
        program_errors: defaultdict = defaultdict(set)
        error_times: defaultdict = defaultdict(list)

        for r in rows:
            err = r.get("RUNTIME_ERR") or "UNKNOWN"
            prog = r.get("REPID") or "UNKNOWN"
            user = r.get("UNAME") or "UNKNOWN"
            error_counts[err] += 1
            program_counts[prog] += 1
            user_counts[user] += 1
            error_programs[err].add(prog)
            error_users[err].add(user)
            program_errors[prog].add(err)
            dt = parse_sap_date(r.get("DATUM", ""))
            if dt:
                t = r.get("UZEIT", "000000")
                error_times[err].append(dt.strftime("%Y%m%d") + str(t))

        last_24h_max = _last_24h_max_by_error(rows)
        repeat_count = sum(1 for c in error_counts.values() if c > 3)
        risk = _overall_risk(total, repeat_count, last_24h_max)

        # Health status
        if risk == "CRITICAL":
            health = "CRITICAL"
        elif risk == "HIGH":
            health = "DEGRADED"
        elif risk == "MEDIUM":
            health = "WARNING"
        else:
            health = "HEALTHY"

        # Build issues sorted by count
        issues = []
        for err, count in error_counts.most_common():
            urgency = "IMMEDIATE" if count > 10 else ("HIGH" if count > 5 else "NORMAL")
            issues.append({
                "error_type": err,
                "count": count,
                "affected_programs": sorted(error_programs[err]),
                "affected_users": sorted(error_users[err]),
                "likely_cause": _likely_cause(err),
                "urgency": urgency,
            })

        primary_issue = issues[0] if issues else None
        secondary_issues = issues[1:5] if len(issues) > 1 else []

        # Pattern detection
        patterns = []
        all_users = set()
        batch_users = set()
        for r in rows:
            u = r.get("UNAME", "")
            all_users.add(u)
            if any(kw in u.upper() for kw in ("BATCH", "BG", "RFC", "JOB", "SCHED")):
                batch_users.add(u)

        # Check for regular intervals (same error repeating)
        for err, count in error_counts.items():
            if count >= 3:
                times = sorted(error_times.get(err, []))
                if len(times) >= 3:
                    patterns.append(f"Error '{err}' repeating {count} times — likely scheduled job causing dumps")
                    break

        # Multiple programs, same error
        for err, progs in error_programs.items():
            if len(progs) >= 3:
                patterns.append(f"{len(progs)} different programs failing with {err} — possible system-level issue (memory/DB)")

        # One program, multiple errors
        for prog, errs in program_errors.items():
            if len(errs) >= 3:
                patterns.append(f"Program '{prog}' failing with {len(errs)} different errors — likely complex program bug")

        # Only one user affected
        if len(all_users) == 1:
            u = list(all_users)[0]
            patterns.append(f"Only user '{u}' affected — likely user-specific authorization or config issue")

        # Only batch users
        if batch_users and batch_users == all_users:
            patterns.append("Only batch/RFC users affected — likely background job configuration issue")

        # Spike detection: check if >60% of dumps are from the last 24 hours
        cutoff_24h = datetime.now() - timedelta(hours=24)
        recent = sum(1 for r in rows if parse_sap_date(r.get("DATUM", "")) and parse_sap_date(r.get("DATUM", "")) >= cutoff_24h)
        if total > 5 and recent > total * 0.6:
            patterns.append(f"Spike: {recent}/{total} dumps in last 24 hours — recent change may have introduced regression")

        if not patterns:
            patterns.append("No strong patterns detected — review individual dumps in ST22")

        # Root cause hypothesis
        if primary_issue:
            top_err = primary_issue["error_type"]
            top_progs = primary_issue["affected_programs"]
            if len(error_programs.get(top_err, set())) >= 3:
                hypothesis = f"Most likely cause: system-level issue affecting multiple programs. Primary error '{top_err}' — {_likely_cause(top_err)}"
            elif len(all_users) == 1:
                hypothesis = f"Most likely cause: user-specific issue. All dumps from user '{list(all_users)[0]}' — check authorizations and user settings."
            elif batch_users and batch_users == all_users:
                hypothesis = f"Most likely cause: background job misconfiguration. Only batch users affected with error '{top_err}'."
            else:
                hypothesis = f"Most likely cause: {_likely_cause(top_err)} Primary error '{top_err}' in program(s) {', '.join(top_progs[:3])}."
        else:
            hypothesis = "No dumps to analyze."

        # Action plan
        action_plan = []
        if primary_issue:
            action_plan.append(f"1. Immediate: Review error '{primary_issue['error_type']}' in ST22 and check program {primary_issue['affected_programs'][0] if primary_issue['affected_programs'] else 'N/A'}")
            if risk in ("CRITICAL", "HIGH"):
                action_plan.append("2. Short term: Disable or fix the most affected program to stop recurring dumps")
            if recent > total * 0.6 and total > 5:
                action_plan.append("3. Investigate: Check recent transports and changes in the last 24-48 hours that may have caused the spike")
            elif repeat_count > 0:
                action_plan.append("3. Investigate: Set up SM21 monitoring for recurring error patterns")
            if secondary_issues:
                action_plan.append(f"4. Follow-up: Address {len(secondary_issues)} secondary issue(s): {', '.join(i['error_type'] for i in secondary_issues)}")

        if not action_plan:
            action_plan.append("1. No immediate action required — system is healthy")

        rec = f"Health: {health}. {total} dump(s) in last {days * 24}h. "
        if primary_issue:
            rec += f"Top issue: {primary_issue['error_type']} ({primary_issue['count']} occurrences). {_likely_cause(primary_issue['error_type'])}"
        else:
            rec += "No issues detected."

        return {
            "source": "live_sap",
            "analysis_period_hours": days * 24,
            "total_dumps": total,
            "health_status": health,
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues,
            "patterns_detected": patterns,
            "root_cause_hypothesis": hypothesis,
            "action_plan": action_plan,
            "overall_risk": risk,
            "recommendation": rec,
        }
