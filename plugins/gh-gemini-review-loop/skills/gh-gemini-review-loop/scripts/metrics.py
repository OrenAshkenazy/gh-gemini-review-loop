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
    if not path.exists():
        return [], 0
    records: list[dict[str, Any]] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
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


def format_run_summary(record: dict[str, Any]) -> str:
    lines = [
        "[loop] Summary",
        f"Findings fetched: {record['findings_fetched']}",
        f"Fixed: {record['fixed_count']}",
    ]
    judge = record.get("judge") or {}
    if judge.get("enabled"):
        verdicts = judge.get("verdicts", {})
        ignored = (
            verdicts.get("false_positive", 0)
            + verdicts.get("duplicate", 0)
            + verdicts.get("already_addressed", 0)
            + verdicts.get("explanation_only", 0)
        )
        lines.append(f"Ignored by judge: {ignored}")
        lines.append(f"Needs human (judge): {verdicts.get('needs_human', 0)}")
    lines.append(f"Needs human: {record['needs_human']}")
    if record.get("addressed_by_reply"):
        lines.append(f"Addressed by reply: {record['addressed_by_reply']}")
    lines.append(f"Cycles used: {record['cycles_used']}/{record['cycle_cap']}")
    lines.append(f"Verification: {record['verification']}")
    lines.append(f"Time to clean PR: {format_duration(record['duration_seconds'])}")
    return "\n".join(lines)


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
    ts: str | None = None,
) -> dict[str, Any]:
    ts = ts or now_iso()
    if not started_at:
        started_at = ts
        duration = 0
    else:
        duration = _duration_seconds(started_at, ts)
    paths = list(finding_paths or [])
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
        "finding_areas": [top_dir(p) for p in paths],
        "finding_paths": paths,
        "judge": judge or {"enabled": False},
    }
