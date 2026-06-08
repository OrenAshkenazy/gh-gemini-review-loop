"""Local, identity-free run metrics for the Gemini review loop.

Pure module: no network, no imports from fetch_gemini_threads. Owns the
runs.jsonl schema, append/load, run-summary formatting, and aggregation.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

RECORD_SCHEMA_VERSION = 1
DEFAULT_WINDOW = 10

JUDGE_VERDICTS = (
    "valid_actionable",
    "false_positive",
    "needs_human",
    "explanation_only",
    "duplicate",
    "already_addressed",
)
JUDGE_ACTIONS = ("fix", "reply", "ignore", "escalate")
VALID_OUTCOMES = (
    "clean",
    "capped",
    "human",
    "regression",
    "no_progress",
    "verification_failed",
)
VALID_VERIFICATION = ("passed", "failed", "skipped")
# Terminal outcomes counted as a "failed run" in the elapsed-by-outcome split.
FAILED_OUTCOMES = ("verification_failed", "regression")

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def runs_log_path() -> Path:
    """Path to runs.jsonl, beside state.json; override via GGRL_STATE_DIR."""
    base = os.environ.get("GGRL_STATE_DIR") or os.path.expanduser(
        "~/.config/gh-gemini-review-loop"
    )
    return Path(base) / "runs.jsonl"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime(_TS_FMT)


def top_dir(path: str) -> str:
    """First path segment of a repo-relative path, or '(unknown)' if empty."""
    if not path:
        return "(unknown)"
    parts = Path(path).parts
    return parts[0] if parts else "(unknown)"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def append_record(record: dict[str, Any], path: Path | None = None) -> None:
    """Append one record as a JSON line. Caller wraps for failure isolation."""
    path = path or runs_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def load_records(path: Path | None = None) -> tuple[list[dict[str, Any]], int]:
    """Return (records, skipped). Skips blank lines silently; counts corrupt
    lines and records whose schema_version this code does not understand."""
    path = path or runs_log_path()
    try:
        if not path.exists():
            return [], 0
        content = path.read_text(encoding="utf-8")
    except OSError:
        return [], 0
    records: list[dict[str, Any]] = []
    skipped = 0
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(rec, dict) or rec.get("schema_version") != RECORD_SCHEMA_VERSION:
            skipped += 1
            continue
        records.append(rec)
    return records, skipped


def build_judge_block(judge_ran: bool, judge_results: dict[str, Any]) -> dict[str, Any]:
    if not judge_ran:
        return {"enabled": False}
    verdicts = {v: 0 for v in JUDGE_VERDICTS}
    actions = {a: 0 for a in JUDGE_ACTIONS}
    for result in judge_results.values():
        verdict = result.get("verdict")
        if verdict in verdicts:
            verdicts[verdict] += 1
        action = result.get("recommended_action")
        if action in actions:
            actions[action] += 1
    return {"enabled": True, "verdicts": verdicts, "recommended_actions": actions}


def _duration_seconds(started_at: str, ts: str) -> int:
    try:
        start = _dt.datetime.strptime(started_at, _TS_FMT)
        end = _dt.datetime.strptime(ts, _TS_FMT)
    except (TypeError, ValueError):
        return 0
    return max(0, int((end - start).total_seconds()))


def format_auto_snapshot(record: dict[str, Any]) -> str:
    """Lean receipt for the Stop-hook backstop (agent didn't post a summary).

    The hook can't know agent-only facts — fixed count, verification result,
    terminal outcome — so this shows ONLY GitHub-observable state and is
    explicitly labelled automatic. It never prints a guessed Fixed/
    Verification/Outcome, which is what made the full receipt misleading when
    fired without agent inputs.
    """
    # One line on purpose: Claude Code collapses multi-line hook output behind
    # "ctrl+o to expand", so a multi-line backstop summary wouldn't actually be
    # visible to someone watching the chat.
    return (
        "[loop] Summary (auto, agent didn't post one): "
        f"{record['findings_fetched']} seen, "
        f"{record.get('observed_fixed_count', 0)} resolved, "
        f"{record['remaining_actionable']} open · "
        f"cycles {record['cycles_used']}/{record['cycle_cap']}"
    )


def format_run_summary(record: dict[str, Any], *, terminal: bool = True) -> str:
    """Human-readable receipt for one loop run.

    A receipt, not a dashboard: a small fixed core, plus optional lines that
    appear only when they carry signal (observed != fixed, judge verdicts > 0,
    addressed-by-reply > 0, a named failed check).

    ``terminal=True`` (default) is the ``--record-run`` path; the header reads
    ``[loop] Summary``. ``terminal=False`` is the mid-loop ``--cycle-summary``
    path; the header reads ``[loop] Cycle receipt`` so users can distinguish
    per-cycle snapshots from the final terminal receipt.
    """
    header = "[loop] Summary" if terminal else "[loop] Cycle receipt"
    lines = [
        header,
        f"Findings fetched: {record['findings_fetched']}",
        f"Fixed: {record['fixed_count']}",
    ]
    observed = record.get("observed_fixed_count")
    if observed is not None and observed != record["fixed_count"]:
        lines.append(f"Observed fixed: {observed}")
    judge = record.get("judge") or {}
    if judge.get("enabled"):
        verdicts = judge.get("verdicts", {})
        ignored = (
            verdicts.get("false_positive", 0)
            + verdicts.get("duplicate", 0)
            + verdicts.get("already_addressed", 0)
            + verdicts.get("explanation_only", 0)
        )
        if ignored:
            lines.append(f"Ignored by judge: {ignored}")
    valid_remaining = record.get("valid_actionable_remaining", record["remaining_actionable"])
    needs_human = record.get("needs_human", 0)
    if valid_remaining:
        lines.append(f"Remaining valid actionable: {valid_remaining}")
    if needs_human:
        lines.append(f"Human decision required: {needs_human}")
    if record.get("addressed_by_reply"):
        lines.append(f"Addressed by reply: {record['addressed_by_reply']}")
    lines.append(f"Cycles used: {record['cycles_used']}/{record['cycle_cap']}")
    lines.append(f"Verification: {record['verification']}")
    if record["verification"] == "failed":
        details = record.get("verification_details")
        failed_check = details.get("failed_check") if isinstance(details, dict) else None
        if failed_check:
            lines.append(f"Failed check: {failed_check}")
    lines.append(f"Outcome: {record['outcome']}")
    label = "Time to clean PR" if record["outcome"] == "clean" else "Time spent"
    lines.append(f"{label}: {format_duration(record['duration_seconds'])}")
    return "\n".join(lines)


def format_suite_block(verification_details: dict[str, Any] | None) -> str:
    """One line per detected verification check, or '' when none.

    Surfaces the repo-aware test toolset (e.g. ``uv run pytest``) that
    ``run_profile.py`` detected, so it is visible in the chat receipt instead
    of buried in the collapsed profile-runner JSON. ``verification_details`` is
    the parsed ``ProfileRunResult.to_details()`` dict.
    """
    if not isinstance(verification_details, dict):
        return ""
    checks = verification_details.get("checks")
    if not isinstance(checks, list) or not checks:
        return ""
    lines = ["Verification suite:"]
    for check in checks:
        if not isinstance(check, dict):
            continue
        command = check.get("command", "?")
        name = check.get("name", "?")
        scope = "required" if check.get("required") else "optional"
        status = check.get("status", "?")
        lines.append(f"  - {command}  ({name}, {scope}) → {status}")
    return "\n".join(lines) if len(lines) > 1 else ""


def format_findings_block(findings: list[dict[str, Any]]) -> str:
    """Deterministic list of the current actionable findings, or '' when empty.

    Each finding dict carries ``path``, ``line``, ``severity``, optional ``url``
    (the GitHub comment permalink), and ``carried`` (True when the same finding
    was already seen in a prior cycle). The header counts new vs carried-over so
    a re-posted finding is never mistaken for a fresh one — e.g. a cycle that
    fetches 4 threads where 1 repeats a prior fix reads ``3 new, 1 carried
    over``.
    """
    valid_findings = [f for f in findings if isinstance(f, dict)]
    if not valid_findings:
        return ""
    new_count = sum(1 for f in valid_findings if not f.get("carried"))
    carried_count = len(valid_findings) - new_count
    lines = [f"Findings ({len(valid_findings)}): {new_count} new, {carried_count} carried over"]
    for idx, finding in enumerate(valid_findings, 1):
        loc = finding.get("path") or "?"
        line = finding.get("line")
        if line is not None:
            loc = f"{loc}:{line}"
        severity = finding.get("severity") or "unknown"
        tag = " · carried over from a prior cycle" if finding.get("carried") else ""
        lines.append(f"  {idx}. {loc} [{severity}]{tag}")
        url = finding.get("url")
        if url:
            lines.append(f"     {url}")
    return "\n".join(lines)


def _mode(items: list[Any]) -> Any | None:
    if not items:
        return None
    return Counter(items).most_common(1)[0][0]


def _avg(values: list[float]) -> float | None:
    """Mean of values, or None when empty. Values are pre-filtered by caller."""
    return (sum(values) / len(values)) if values else None


def _is_number(x: Any) -> bool:
    """True for a real numeric duration: int/float but not bool.

    Records come from a local JSONL log that can be hand-edited or corrupted,
    so a duration could be a string, None, or bool. Validating the type keeps
    aggregation from crashing on summation. ``bool`` is excluded because
    True/False are not meaningful durations even though they are ``int``.
    """
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _elapsed_for(records: list[dict[str, Any]], outcomes: tuple[str, ...]) -> float | None:
    """Average duration_seconds over records whose outcome is in ``outcomes``.

    Durations are validated numeric (an explicit check that also keeps a valid
    0-second run, which a truthiness test would drop).
    """
    vals = [
        r["duration_seconds"]
        for r in records
        if r.get("outcome") in outcomes and _is_number(r.get("duration_seconds"))
    ]
    return _avg(vals)


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    if n == 0:
        return {"count": 0}
    # Every field below is type-validated: records come from a local,
    # hand-editable JSONL log, so a corrupt scalar/list must be skipped rather
    # than crash summation or iteration.
    cycles = [
        r["cycles_used"] if _is_number(r.get("cycles_used")) else 0 for r in records
    ]
    # Validate numeric (keeps a valid 0-second run, which a truthiness test
    # would drop; rejects corrupt string/bool durations that would crash sum()).
    durations = [
        r["duration_seconds"] for r in records if _is_number(r.get("duration_seconds"))
    ]
    judged = [
        r for r in records
        if isinstance(r.get("judge"), dict) and r["judge"].get("enabled")
    ]
    false_pos = 0
    for r in judged:
        verdicts = r["judge"].get("verdicts")
        if isinstance(verdicts, dict) and _is_number(verdicts.get("false_positive")):
            false_pos += verdicts["false_positive"]
    providers = [r.get("provider") for r in records if isinstance(r.get("provider"), str)]
    areas = [
        a
        for r in records
        if isinstance(r.get("finding_areas"), list)
        for a in r["finding_areas"]
        if isinstance(a, str)
    ]

    # Active-cycle metrics: only runs that recorded per-cycle timing. Legacy
    # records without a "cycles" list are excluded from active metrics (but
    # still contribute to the elapsed metrics above).
    # Require a non-empty list: a non-list truthy value (corrupt record) would
    # TypeError on iteration/len(); an empty list means "no recorded cycles".
    runs_with_cycles = [
        r for r in records if isinstance(r.get("cycles"), list) and r["cycles"]
    ]
    # Cycle entries can be corrupt too: keep only dicts, and only numeric
    # durations, so a malformed entry never crashes aggregation.
    all_cycles = [
        c for r in runs_with_cycles for c in r["cycles"] if isinstance(c, dict)
    ]
    cycle_durations = [
        c["duration_seconds"]
        for c in all_cycles
        if _is_number(c.get("duration_seconds"))
    ]
    run_active_totals = [
        sum(
            c["duration_seconds"]
            for c in r["cycles"]
            if isinstance(c, dict) and _is_number(c.get("duration_seconds"))
        )
        for r in runs_with_cycles
    ]
    cycle_counts = [
        sum(1 for c in r["cycles"] if isinstance(c, dict)) for r in runs_with_cycles
    ]

    return {
        "count": n,
        "avg_cycles": sum(cycles) / n,
        # Elapsed (wall-clock) metrics — user-visible latency.
        "avg_duration": _avg(durations),
        "avg_duration_clean": _elapsed_for(records, ("clean",)),
        "avg_duration_capped": _elapsed_for(records, ("capped",)),
        "avg_duration_failed": _elapsed_for(records, FAILED_OUTCOMES),
        # Active-cycle metrics — agent/loop processing efficiency.
        "avg_active_cycle_time": _avg(cycle_durations),
        "avg_active_time_per_run": _avg(run_active_totals),
        "avg_cycles_per_run": _avg(cycle_counts),
        "total_fetched": sum(r.get("findings_fetched", 0) for r in records),
        "total_fixed": sum(r.get("observed_fixed_count", 0) for r in records),
        "needs_human": sum(r.get("needs_human", 0) for r in records),
        "addressed_by_reply": sum(r.get("addressed_by_reply", 0) for r in records),
        "judged_count": len(judged),
        "false_positives_avoided": false_pos,
        "top_provider": _mode(providers),
        "top_area": _mode(areas),
    }


def format_stats(repo: str, stats: dict[str, Any], skipped: int = 0) -> str:
    if stats.get("count", 0) == 0:
        msg = (
            "No Gemini loop runs recorded yet for this repo. "
            "Run the loop once and stats will appear here."
        )
        if skipped:
            msg += f"\n\n({skipped} unreadable record{'s' if skipped != 1 else ''} skipped)"
        return msg
    lines = [f"Gemini loop stats — {repo}", f"Last {stats['count']} runs", ""]
    lines.append(f"Average cycles used: {stats['avg_cycles']:.1f}")

    def _elapsed_line(label: str, key: str) -> None:
        val = stats.get(key)
        if val is not None:
            lines.append(f"{label}: {format_duration(round(val))}")

    # Elapsed (wall-clock) latency — what the user waited, including review
    # latency, polling, and idle gaps. Split by terminal outcome.
    _elapsed_line("Average elapsed time to terminal outcome", "avg_duration")
    _elapsed_line("Average elapsed time to clean PR", "avg_duration_clean")
    _elapsed_line("Average elapsed time to capped run", "avg_duration_capped")
    _elapsed_line("Average elapsed time to failed run", "avg_duration_failed")

    # Active-cycle time — agent/loop processing efficiency, excluding waits.
    # Absent on legacy records (pre-cycle-tracking); omitted then.
    if stats.get("avg_active_cycle_time") is not None:
        lines.append(
            f"Average active cycle time: "
            f"{format_duration(round(stats['avg_active_cycle_time']))}"
        )
    if stats.get("avg_active_time_per_run") is not None:
        lines.append(
            f"Average active time per run: "
            f"{format_duration(round(stats['avg_active_time_per_run']))}"
        )
    if stats.get("avg_cycles_per_run") is not None:
        lines.append(f"Average cycles per run: {stats['avg_cycles_per_run']:.1f}")

    lines.append(f"Findings fixed: {stats['total_fixed']} of {stats['total_fetched']}")
    lines.append(f"Human decisions needed: {stats['needs_human']}")
    if stats["addressed_by_reply"]:
        lines.append(f"Addressed by reply: {stats['addressed_by_reply']}")
    if stats["judged_count"] > 0:
        lines.append(
            f"False positives avoided: {stats['false_positives_avoided']}   "
            f"(across {stats['judged_count']} of {stats['count']} judged runs)"
        )
    if stats["top_provider"]:
        lines.append(f"Most common provider: {stats['top_provider']}")
    if stats["top_area"]:
        lines.append(f"Most repeated finding area: {stats['top_area']}")
    out = "\n".join(lines)
    if skipped:
        out += f"\n\n({skipped} unreadable record{'s' if skipped != 1 else ''} skipped)"
    return out


def build_record(
    *,
    repo: str,
    pr: int,
    provider: str,
    findings_fetched: int,
    fixed_count: int,
    observed_fixed_count: int,
    remaining_actionable: int,
    needs_human: int,
    addressed_by_reply: int,
    cycles_used: int,
    cycle_cap: int,
    verification: str,
    verification_details: dict[str, Any] | None,
    outcome: str,
    outcome_reason: str,
    started_at: str | None,
    finding_paths: list[str],
    judge: dict[str, Any] | None,
    cycles: list[dict[str, Any]] | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    ts = ts or now_iso()
    if not started_at:
        started_at = ts
        duration = 0
    else:
        duration = _duration_seconds(started_at, ts)
    paths = list(finding_paths or [])
    valid_actionable_remaining = max(0, remaining_actionable - needs_human)
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "ts": ts,
        "repo": repo,
        "pr": pr,
        "provider": provider,
        "findings_fetched": findings_fetched,
        "fixed_count": fixed_count,
        "observed_fixed_count": observed_fixed_count,
        "remaining_actionable": remaining_actionable,
        "valid_actionable_remaining": valid_actionable_remaining,
        "needs_human": needs_human,
        "addressed_by_reply": addressed_by_reply,
        "cycles_used": cycles_used,
        "cycle_cap": cycle_cap,
        "verification": verification,
        "verification_details": verification_details or {},
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "started_at": started_at,
        "duration_seconds": duration,
        # Per-cycle active-work timing (excludes waits/idle). Empty on legacy
        # runs and any run that did not record cycle timing. Guard the type: a
        # corrupt accumulator value must not crash list().
        "cycles": list(cycles) if isinstance(cycles, list) else [],
        "finding_areas": [top_dir(p) for p in paths],
        "finding_paths": paths,
        "judge": judge or {"enabled": False},
    }
