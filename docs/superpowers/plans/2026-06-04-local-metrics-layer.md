# Local Metrics Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, identity-free metrics layer to the Gemini review loop — a per-run summary (Feature 1) that persists one record per run, and a `--stats` aggregate (Feature 2) over those records.

**Architecture:** A new pure, tested module `metrics.py` owns the record schema, JSONL append/load, run-summary formatting, and aggregation. `fetch_gemini_threads.py` is the single CLI entry point: it tracks run state in the existing `state.json`, derives counts from thread state, folds in two agent-supplied facts (`--fixed-count`, `--verification`), and on `--record-run` writes one record + prints the summary; on `--stats` it reads the log and prints the aggregate. No network in `metrics.py`; no identity in any record.

**Tech Stack:** Python 3.10+, stdlib only (`json`, `datetime`, `pathlib`, `collections`), pytest. Storage is JSONL at `~/.config/gh-gemini-review-loop/runs.jsonl` (honoring `GGRL_STATE_DIR`).

---

## Implementation note — one refinement vs. the spec

The spec lists `outcome` as script-derived. In practice the script cannot infer *why* the loop stopped (human-decision vs. no-progress vs. regression) — only the agent knows. So this plan adds two **optional** agent inputs, `--outcome` and `--outcome-reason`, with a sensible script-derived default when omitted. This keeps every *headline KPI* (findings, fixed, needs-human counts) script-derived while letting the agent state the one fact only it holds. `--fixed-count` and `--verification` remain as specified. If you disagree, the default-derivation path alone still works (agent passes neither).

`needs_human` derivation (script):
- judge ran → `judge.verdicts.needs_human`
- else if `outcome == "human"` → `remaining_actionable`
- else → `0`

`observed_fixed_count` derivation (script): baseline finding IDs that are no longer actionable and were not deferred-by-reply: `len(baseline_ids - current_actionable_ids - addressed_by_reply_ids)`.

## File Structure

- **Create** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py` — pure module: schema constants, path helper, duration/area helpers, `append_record`, `load_records`, `build_judge_block`, `build_record`, `format_run_summary`, `aggregate`, `format_stats`. No imports from `fetch_gemini_threads`; no network.
- **Modify** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` — run-state tracking helpers (in `state.json`), a repo resolver, new argparse flags, and two new branches in `main()` (`--record-run`, `--stats`).
- **Create** `tests/test_metrics.py` — unit tests for the pure module.
- **Modify** `tests/test_fetch_gemini_threads.py` — tests for run-state tracking + the two new `main()` branches.
- **Modify** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md`, `README.md`, `PRIVACY.md` — docs/wiring.

Conftest (`tests/conftest.py`) already puts `scripts/` on `sys.path`, so `import metrics` and `from fetch_gemini_threads import ...` work in tests.

---

## Task 1: `metrics.py` scaffolding — constants & pure helpers

**Files:**
- Create: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_metrics.py
import metrics


class TestHelpers:
    def test_runs_log_path_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert metrics.runs_log_path() == tmp_path / "runs.jsonl"

    def test_top_dir(self):
        assert metrics.top_dir("tests/test_auth.py") == "tests"
        assert metrics.top_dir("src/auth/login.py") == "src"
        assert metrics.top_dir("") == "(unknown)"

    def test_format_duration(self):
        assert metrics.format_duration(48) == "48s"
        assert metrics.format_duration(720) == "12m"
        assert metrics.format_duration(3840) == "1h 4m"
        assert metrics.format_duration(0) == "0s"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'metrics'`.

- [ ] **Step 3: Write minimal implementation**

```python
# plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_metrics.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): scaffold metrics module with pure helpers"
```

---

## Task 2: `append_record` + `load_records` (JSONL persistence)

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
class TestPersistence:
    def test_append_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        metrics.append_record({"schema_version": 1, "repo": "a/b", "pr": 1})
        metrics.append_record({"schema_version": 1, "repo": "a/b", "pr": 2})
        records, skipped = metrics.load_records()
        assert skipped == 0
        assert [r["pr"] for r in records] == [1, 2]

    def test_load_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert metrics.load_records() == ([], 0)

    def test_load_skips_corrupt_and_future_version(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        path = tmp_path / "runs.jsonl"
        path.write_text(
            '{"schema_version": 1, "pr": 1}\n'
            "not json at all\n"
            '{"schema_version": 999, "pr": 2}\n'
            "\n"
            '{"schema_version": 1, "pr": 3}\n'
        )
        records, skipped = metrics.load_records()
        assert [r["pr"] for r in records] == [1, 3]
        assert skipped == 2  # corrupt line + future version; blank line ignored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_metrics.py::TestPersistence -v`
Expected: FAIL — `AttributeError: module 'metrics' has no attribute 'append_record'`.

- [ ] **Step 3: Write minimal implementation**

Append to `metrics.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_metrics.py::TestPersistence -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): JSONL append + defensive load with skip counting"
```

---

## Task 3: `build_judge_block` + `build_record`

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
class TestBuildJudgeBlock:
    def test_disabled_when_not_run(self):
        assert metrics.build_judge_block(False, {}) == {"enabled": False}

    def test_counts_verdicts_and_actions(self):
        results = {
            "t1": {"verdict": "valid_actionable", "recommended_action": "fix"},
            "t2": {"verdict": "false_positive", "recommended_action": "ignore"},
            "t3": {"verdict": "false_positive", "recommended_action": "ignore"},
            "t4": {"verdict": "needs_human", "recommended_action": "escalate"},
        }
        block = metrics.build_judge_block(True, results)
        assert block["enabled"] is True
        assert block["verdicts"]["false_positive"] == 2
        assert block["verdicts"]["needs_human"] == 1
        assert block["verdicts"]["duplicate"] == 0
        assert block["recommended_actions"]["ignore"] == 2
        assert block["recommended_actions"]["escalate"] == 1


class TestBuildRecord:
    def _kwargs(self, **over):
        base = dict(
            repo="o/r", pr=23, provider="gemini-code-assist",
            findings_fetched=7, fixed_count=4, observed_fixed_count=4,
            remaining_actionable=1, needs_human=1, addressed_by_reply=2,
            cycles_used=2, cycle_cap=3, verification="passed",
            verification_details={}, outcome="clean",
            outcome_reason="0 actionable threads remaining",
            started_at="2026-06-04T18:10:11Z", ts="2026-06-04T18:22:11Z",
            finding_paths=["tests/test_auth.py", "src/auth/login.py"],
            judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_full_record_shape_and_derived_fields(self):
        rec = metrics.build_record(**self._kwargs())
        assert rec["schema_version"] == 1
        assert rec["duration_seconds"] == 720
        assert rec["finding_areas"] == ["tests", "src"]
        assert rec["finding_paths"] == ["tests/test_auth.py", "src/auth/login.py"]
        assert rec["verification_details"] == {}
        assert rec["judge"] == {"enabled": False}

    def test_missing_started_at_falls_back_to_ts(self):
        rec = metrics.build_record(**self._kwargs(started_at=None))
        assert rec["started_at"] == rec["ts"]
        assert rec["duration_seconds"] == 0

    def test_all_outcomes_accepted(self):
        for outcome in metrics.VALID_OUTCOMES:
            rec = metrics.build_record(**self._kwargs(outcome=outcome))
            assert rec["outcome"] == outcome
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_metrics.py::TestBuildRecord tests/test_metrics.py::TestBuildJudgeBlock -v`
Expected: FAIL — `AttributeError: module 'metrics' has no attribute 'build_judge_block'`.

- [ ] **Step 3: Write minimal implementation**

Append to `metrics.py`:

```python
def build_judge_block(judge_ran: bool, judge_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_metrics.py::TestBuildRecord tests/test_metrics.py::TestBuildJudgeBlock -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): build_record + judge block assembly"
```

---

## Task 4: `format_run_summary` (Feature 1 render)

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
class TestFormatRunSummary:
    def _rec(self, **over):
        base = metrics.build_record(
            repo="o/r", pr=23, provider="gemini-code-assist",
            findings_fetched=7, fixed_count=4, observed_fixed_count=4,
            remaining_actionable=1, needs_human=1, addressed_by_reply=0,
            cycles_used=2, cycle_cap=3, verification="passed",
            verification_details={}, outcome="clean", outcome_reason="ok",
            started_at="2026-06-04T18:10:11Z", ts="2026-06-04T18:22:11Z",
            finding_paths=["tests/x.py"], judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_judge_off_omits_judge_lines(self):
        out = metrics.format_run_summary(self._rec())
        assert out.splitlines() == [
            "[loop] Summary",
            "Findings fetched: 7",
            "Fixed: 4",
            "Needs human: 1",
            "Cycles used: 2/3",
            "Verification: passed",
            "Time to clean PR: 12m",
        ]

    def test_addressed_by_reply_line_omitted_when_zero(self):
        assert "Addressed by reply" not in metrics.format_run_summary(self._rec())

    def test_addressed_by_reply_line_shown_when_nonzero(self):
        out = metrics.format_run_summary(self._rec(addressed_by_reply=2))
        assert "Addressed by reply: 2" in out

    def test_judge_on_inserts_two_judge_lines_after_fixed(self):
        judge = {
            "enabled": True,
            "verdicts": {
                "valid_actionable": 3, "false_positive": 1, "duplicate": 1,
                "already_addressed": 1, "explanation_only": 0, "needs_human": 1,
            },
            "recommended_actions": {"fix": 3, "reply": 1, "ignore": 2, "escalate": 1},
        }
        out = metrics.format_run_summary(self._rec(judge=judge)).splitlines()
        assert out[2] == "Fixed: 4"
        assert out[3] == "Ignored by judge: 3"   # false_positive+duplicate+already_addressed+explanation_only
        assert out[4] == "Needs human (judge): 1"
        assert out[5] == "Needs human: 1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_metrics.py::TestFormatRunSummary -v`
Expected: FAIL — `AttributeError: module 'metrics' has no attribute 'format_run_summary'`.

- [ ] **Step 3: Write minimal implementation**

Append to `metrics.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_metrics.py::TestFormatRunSummary -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): format_run_summary with judge/addressed-by-reply omission rules"
```

---

## Task 5: `aggregate` + `format_stats` (Feature 2)

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
class TestAggregate:
    def _rec(self, **over):
        base = dict(
            schema_version=1, repo="o/r", pr=1, provider="gemini-code-assist",
            findings_fetched=5, observed_fixed_count=4, needs_human=1,
            addressed_by_reply=1, cycles_used=2, duration_seconds=600,
            finding_areas=["tests"], judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_empty_returns_count_zero(self):
        assert metrics.aggregate([]) == {"count": 0}

    def test_basic_aggregation(self):
        recs = [
            self._rec(cycles_used=2, duration_seconds=600, observed_fixed_count=4, findings_fetched=5),
            self._rec(cycles_used=1, duration_seconds=1200, observed_fixed_count=3, findings_fetched=4),
        ]
        agg = metrics.aggregate(recs)
        assert agg["count"] == 2
        assert agg["avg_cycles"] == 1.5
        assert agg["avg_duration"] == 900.0
        assert agg["total_fixed"] == 7
        assert agg["total_fetched"] == 9
        assert agg["top_provider"] == "gemini-code-assist"
        assert agg["top_area"] == "tests"

    def test_duration_zero_excluded_from_average(self):
        recs = [self._rec(duration_seconds=0), self._rec(duration_seconds=600)]
        assert metrics.aggregate(recs)["avg_duration"] == 600.0

    def test_false_positives_only_over_judged_runs(self):
        judged = self._rec(judge={"enabled": True, "verdicts": {"false_positive": 3}})
        unjudged = self._rec(judge={"enabled": False})
        agg = metrics.aggregate([judged, unjudged])
        assert agg["judged_count"] == 1
        assert agg["false_positives_avoided"] == 3


class TestFormatStats:
    def test_empty_message(self):
        out = metrics.format_stats("o/r", {"count": 0})
        assert "No Gemini loop runs recorded yet" in out

    def test_full_output_with_judge_footnote(self):
        agg = {
            "count": 10, "avg_cycles": 1.8, "avg_duration": 540.0,
            "total_fixed": 32, "total_fetched": 41, "needs_human": 6,
            "addressed_by_reply": 9, "judged_count": 6,
            "false_positives_avoided": 14, "top_provider": "gemini-code-assist",
            "top_area": "tests",
        }
        out = metrics.format_stats("OrenAshkenazy/gh-gemini-review-loop", agg)
        assert "Last 10 runs" in out
        assert "Average cycles used: 1.8" in out
        assert "Average time to clean PR: 9m" in out
        assert "Findings fixed: 32 of 41" in out
        assert "False positives avoided: 14   (across 6 of 10 judged runs)" in out
        assert "Most repeated finding area: tests" in out

    def test_judge_line_omitted_when_no_judged_runs(self):
        agg = {
            "count": 2, "avg_cycles": 1.0, "avg_duration": None,
            "total_fixed": 1, "total_fetched": 2, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": "gemini-code-assist",
            "top_area": None,
        }
        out = metrics.format_stats("o/r", agg)
        assert "False positives avoided" not in out
        assert "Average time to clean PR" not in out  # avg_duration is None

    def test_skipped_footnote(self):
        agg = {
            "count": 1, "avg_cycles": 1.0, "avg_duration": None,
            "total_fixed": 0, "total_fetched": 0, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": None, "top_area": None,
        }
        out = metrics.format_stats("o/r", agg, skipped=2)
        assert "(2 unreadable records skipped)" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_metrics.py::TestAggregate tests/test_metrics.py::TestFormatStats -v`
Expected: FAIL — `AttributeError: module 'metrics' has no attribute 'aggregate'`.

- [ ] **Step 3: Write minimal implementation**

Append to `metrics.py`:

```python
def _mode(items: list[Any]) -> Any | None:
    if not items:
        return None
    return Counter(items).most_common(1)[0][0]


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    if n == 0:
        return {"count": 0}
    cycles = [r.get("cycles_used", 0) for r in records]
    durations = [r["duration_seconds"] for r in records if r.get("duration_seconds")]
    judged = [r for r in records if (r.get("judge") or {}).get("enabled")]
    false_pos = sum(
        (r["judge"].get("verdicts", {}) or {}).get("false_positive", 0) for r in judged
    )
    providers = [r.get("provider") for r in records if r.get("provider")]
    areas = [a for r in records for a in (r.get("finding_areas") or [])]
    return {
        "count": n,
        "avg_cycles": sum(cycles) / n,
        "avg_duration": (sum(durations) / len(durations)) if durations else None,
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
    if stats["avg_duration"] is not None:
        lines.append(
            f"Average time to clean PR: {format_duration(round(stats['avg_duration']))}"
        )
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_metrics.py -v`
Expected: PASS (all metrics tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): aggregate + format_stats for Feature 2"
```

---

## Task 6: Run-state tracking in `state.json`

Tracks the loop's start timestamp and the union of finding IDs/paths seen across cycles, under the existing `owner/repo#number` key.

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (add helpers near `load_sticky_state`/`save_sticky_state`, ~line 609)
- Test: `tests/test_fetch_gemini_threads.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_fetch_gemini_threads.py` (import the new names at the top alongside existing imports):

```python
from fetch_gemini_threads import (
    PullRequest,
    update_run_tracking,
    read_run_tracking,
    clear_run_tracking,
)


class TestRunTracking:
    def _pr(self):
        return PullRequest(owner="o", repo="r", number=1)

    def test_first_update_sets_started_at_and_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(self._pr(), [("t1", "a.py"), ("t2", "b.py")])
        run = read_run_tracking(self._pr())
        assert "started_at" in run
        assert run["finding_ids"] == ["t1", "t2"]
        assert run["finding_paths"] == ["a.py", "b.py"]

    def test_second_update_unions_and_preserves_started_at(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        first_started = read_run_tracking(pr)["started_at"]
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        run = read_run_tracking(pr)
        assert run["started_at"] == first_started
        assert run["finding_ids"] == ["t1", "t2"]

    def test_clear_removes_run_but_keeps_other_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        from fetch_gemini_threads import load_sticky_state, save_sticky_state, _state_key
        save_sticky_state({_state_key(pr): {"commentId": 42}})
        update_run_tracking(pr, [("t1", "a.py")])
        clear_run_tracking(pr)
        assert read_run_tracking(pr) == {}
        assert load_sticky_state()[_state_key(pr)]["commentId"] == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fetch_gemini_threads.py::TestRunTracking -v`
Expected: FAIL — `ImportError: cannot import name 'update_run_tracking'`.

- [ ] **Step 3: Write minimal implementation**

Add to `fetch_gemini_threads.py` after `save_sticky_state` (around line 609):

```python
def update_run_tracking(pr: PullRequest, findings: list[tuple[str, str | None]]) -> None:
    """Merge this invocation's findings into the run's tracking state.

    ``findings`` is a list of (thread_id, path) pairs. Sets ``started_at`` on
    the first call of a run; unions ids/paths on every call. Stored under the
    existing ``owner/repo#number`` key so it rides alongside sticky state.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    run = entry.get("run", {})
    if "started_at" not in run:
        run["started_at"] = _now_iso()
    ids = set(run.get("finding_ids", []))
    paths = set(run.get("finding_paths", []))
    for thread_id, path in findings:
        if thread_id:
            ids.add(thread_id)
        if path:
            paths.add(path)
    run["finding_ids"] = sorted(ids)
    run["finding_paths"] = sorted(paths)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)


def read_run_tracking(pr: PullRequest) -> dict[str, Any]:
    return load_sticky_state().get(_state_key(pr), {}).get("run", {})


def clear_run_tracking(pr: PullRequest) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    if key in state and "run" in state[key]:
        del state[key]["run"]
        save_sticky_state(state)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fetch_gemini_threads.py::TestRunTracking -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat(metrics): run-state tracking (started_at + finding ids/paths) in state.json"
```

---

## Task 7: Wire `--record-run` into `main()`

Adds the flags, derives counts from thread state + run-tracking + judge, builds and appends the record, prints the summary, and clears run-state. Failure to write degrades to a warning.

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (argparse block ~line 951; `main()` branch before the `--format` render ~line 1131)
- Test: `tests/test_fetch_gemini_threads.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_fetch_gemini_threads.py`. This test exercises the derivation helper directly (pure, no network):

```python
from fetch_gemini_threads import derive_record_fields


class TestDeriveRecordFields:
    def test_observed_fixed_and_findings_and_needs_human(self):
        # baseline saw t1,t2,t3,t4; now t1 still actionable, t2 addressed-by-reply,
        # t3 & t4 gone (presumed fixed). judge off, outcome human.
        fields = derive_record_fields(
            baseline_ids={"t1", "t2", "t3", "t4"},
            current_actionable_ids={"t1"},
            addressed_by_reply_ids={"t2"},
            outcome="human",
            judge_ran=False,
            judge_results={},
        )
        assert fields["findings_fetched"] == 4
        assert fields["observed_fixed_count"] == 2          # t3, t4
        assert fields["remaining_actionable"] == 1          # t1
        assert fields["addressed_by_reply"] == 1            # t2
        assert fields["needs_human"] == 1                   # outcome human -> remaining_actionable

    def test_needs_human_from_judge_when_judge_ran(self):
        fields = derive_record_fields(
            baseline_ids={"t1"},
            current_actionable_ids={"t1"},
            addressed_by_reply_ids=set(),
            outcome="clean",
            judge_ran=True,
            judge_results={"t1": {"verdict": "needs_human", "recommended_action": "escalate"}},
        )
        assert fields["needs_human"] == 1

    def test_needs_human_zero_when_not_human_and_no_judge(self):
        fields = derive_record_fields(
            baseline_ids={"t1"},
            current_actionable_ids=set(),
            addressed_by_reply_ids=set(),
            outcome="clean",
            judge_ran=False,
            judge_results={},
        )
        assert fields["needs_human"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fetch_gemini_threads.py::TestDeriveRecordFields -v`
Expected: FAIL — `ImportError: cannot import name 'derive_record_fields'`.

- [ ] **Step 3: Write minimal implementation**

3a. Add the pure derivation helper to `fetch_gemini_threads.py` (near the other thread helpers, e.g. after `severity_counts` ~line 364):

```python
def derive_record_fields(
    *,
    baseline_ids: set[str],
    current_actionable_ids: set[str],
    addressed_by_reply_ids: set[str],
    outcome: str,
    judge_ran: bool,
    judge_results: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Compute the script-derived metric counts for a run record."""
    findings_fetched = len(baseline_ids | current_actionable_ids)
    observed_fixed_count = len(
        baseline_ids - current_actionable_ids - addressed_by_reply_ids
    )
    remaining_actionable = len(current_actionable_ids)
    addressed_by_reply = len(addressed_by_reply_ids)
    if judge_ran:
        needs_human = sum(
            1
            for r in judge_results.values()
            if r.get("verdict") == "needs_human"
        )
    elif outcome == "human":
        needs_human = remaining_actionable
    else:
        needs_human = 0
    return {
        "findings_fetched": findings_fetched,
        "observed_fixed_count": observed_fixed_count,
        "remaining_actionable": remaining_actionable,
        "addressed_by_reply": addressed_by_reply,
        "needs_human": needs_human,
    }


def _derive_outcome(remaining_actionable: int, verification: str, cap_reached: bool) -> str:
    if cap_reached:
        return "capped"
    if verification == "failed":
        return "verification_failed"
    if remaining_actionable == 0 and verification == "passed":
        return "clean"
    return "human"
```

3b. Add the argparse flags after the `--judge-model` argument (~line 955):

```python
    parser.add_argument(
        "--record-run",
        action="store_true",
        help=(
            "Write one run-metrics record to runs.jsonl and print the [loop] Summary. "
            "Use once at loop end. Combine with --fixed-count and --verification."
        ),
    )
    parser.add_argument("--fixed-count", type=int, default=0, help="Agent-claimed fixes this run.")
    parser.add_argument(
        "--verification",
        choices=["passed", "failed", "skipped"],
        default="skipped",
        help="Result of the verification step this run.",
    )
    parser.add_argument(
        "--verification-details",
        default=None,
        help="Optional JSON object with structured verification context.",
    )
    parser.add_argument(
        "--outcome",
        choices=list(metrics.VALID_OUTCOMES),
        default=None,
        help="Terminal outcome of the loop. If omitted, derived from state.",
    )
    parser.add_argument("--outcome-reason", default=None, help="One-line reason for --outcome.")
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print local Gemini-loop stats for this repo from runs.jsonl and exit.",
    )
    parser.add_argument(
        "--stats-window",
        type=int,
        default=metrics.DEFAULT_WINDOW,
        help=f"Number of most-recent runs to aggregate. Default: {metrics.DEFAULT_WINDOW}.",
    )
    parser.add_argument(
        "--stats-all-repos",
        action="store_true",
        help="Aggregate across all repos instead of only the current one.",
    )
```

3c. Add `import metrics` at the top of `fetch_gemini_threads.py` with the other imports.

3d. In `main()`, after the threads/judge/rereviews are computed and before the `--post-receipt` block (~line 1131), add the record branch. It runs only with `--record-run`:

```python
        if args.record_run:
            update_run_tracking(
                pr, [(t["id"], t.get("path")) for t in threads]
            )
            run = read_run_tracking(pr)
            baseline_ids = set(run.get("finding_ids", []))
            finding_paths = run.get("finding_paths", [])
            current_actionable_ids = {t["id"] for t in threads}
            addressed_by_reply_ids = {
                t["id"] for t in addressed_by_reply_threads(pull_request, args.author)
            }
            judge_ran = bool(judge_status.get("ran"))
            cap_reached = len(rereviews) >= args.max_rereview_requests
            outcome = args.outcome or _derive_outcome(
                len(current_actionable_ids), args.verification, cap_reached
            )
            derived = derive_record_fields(
                baseline_ids=baseline_ids,
                current_actionable_ids=current_actionable_ids,
                addressed_by_reply_ids=addressed_by_reply_ids,
                outcome=outcome,
                judge_ran=judge_ran,
                judge_results=judge_results,
            )
            verification_details: dict[str, Any] = {}
            if args.verification_details:
                try:
                    verification_details = json.loads(args.verification_details)
                except json.JSONDecodeError:
                    print(
                        "warning: --verification-details is not valid JSON; storing {}.",
                        file=sys.stderr,
                    )
            record = metrics.build_record(
                repo=f"{pr.owner}/{pr.repo}",
                pr=pr.number,
                provider=args.author,
                fixed_count=args.fixed_count,
                cycles_used=len(rereviews),
                cycle_cap=args.max_rereview_requests,
                verification=args.verification,
                verification_details=verification_details,
                outcome=outcome,
                outcome_reason=args.outcome_reason or f"outcome: {outcome}",
                started_at=run.get("started_at"),
                finding_paths=finding_paths,
                judge=metrics.build_judge_block(judge_ran, judge_results),
                **derived,
            )
            try:
                metrics.append_record(record)
            except OSError as exc:
                print(f"warning: could not record run metrics: {exc}", file=sys.stderr)
            else:
                clear_run_tracking(pr)
            print(metrics.format_run_summary(record))
            return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fetch_gemini_threads.py::TestDeriveRecordFields -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the whole suite to confirm no regressions**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all existing + new tests). If `import metrics` ordering or argparse defaults broke an existing test, fix before committing.

- [ ] **Step 6: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat(metrics): wire --record-run (derive counts, append record, print summary)"
```

---

## Task 8: Wire `--stats` into `main()`

Short-circuits before any network call: resolve the repo offline when possible, read the log, aggregate the most-recent window, print.

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (early in `main()`, right after `args = parser.parse_args()` ~line 956; add a repo resolver helper)
- Test: `tests/test_fetch_gemini_threads.py`

- [ ] **Step 1: Write the failing test**

```python
from fetch_gemini_threads import select_stats_records


class TestSelectStatsRecords:
    def _rec(self, repo, pr):
        return {"schema_version": 1, "repo": repo, "pr": pr}

    def test_filters_to_repo_and_takes_window(self):
        recs = [
            self._rec("o/r", 1), self._rec("x/y", 2),
            self._rec("o/r", 3), self._rec("o/r", 4),
        ]
        out = select_stats_records(recs, repo="o/r", window=2, all_repos=False)
        assert [r["pr"] for r in out] == [3, 4]   # last 2 for o/r, file order

    def test_all_repos_keeps_everything_in_window(self):
        recs = [self._rec("o/r", 1), self._rec("x/y", 2), self._rec("o/r", 3)]
        out = select_stats_records(recs, repo="o/r", window=2, all_repos=True)
        assert [r["pr"] for r in out] == [2, 3]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fetch_gemini_threads.py::TestSelectStatsRecords -v`
Expected: FAIL — `ImportError: cannot import name 'select_stats_records'`.

- [ ] **Step 3: Write minimal implementation**

3a. Add the pure selector + a repo resolver to `fetch_gemini_threads.py`:

```python
def select_stats_records(
    records: list[dict[str, Any]], *, repo: str, window: int, all_repos: bool
) -> list[dict[str, Any]]:
    if not all_repos:
        records = [r for r in records if r.get("repo") == repo]
    return records[-window:] if window > 0 else records


def resolve_current_repo() -> str:
    """Return 'owner/repo' for the current dir without needing an open PR."""
    view = run_gh(["repo", "view", "--json", "nameWithOwner"])
    if not isinstance(view, dict) or "nameWithOwner" not in view:
        raise RuntimeError("Could not resolve the current repo with gh repo view.")
    return view["nameWithOwner"]
```

3b. In `main()`, immediately after `args = parser.parse_args()` (~line 956), add the stats short-circuit (before the `try:` that does network fetch):

```python
    if args.stats:
        try:
            if args.pr:
                pr = resolve_pr(args.pr)
                repo_full = f"{pr.owner}/{pr.repo}"
            elif args.stats_all_repos:
                repo_full = "(all repos)"
            else:
                repo_full = resolve_current_repo()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        records, skipped = metrics.load_records()
        selected = select_stats_records(
            records, repo=repo_full, window=args.stats_window, all_repos=args.stats_all_repos
        )
        agg = metrics.aggregate(selected)
        if args.format == "json":
            print(json.dumps({"repo": repo_full, "stats": agg, "skipped": skipped}, indent=2, sort_keys=True))
        else:
            print(metrics.format_stats(repo_full, agg, skipped=skipped))
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fetch_gemini_threads.py::TestSelectStatsRecords -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Offline end-to-end smoke check**

Run (empty log → friendly message, no network because `--pr` is offline-parseable):

```bash
GGRL_STATE_DIR="$(mktemp -d)" python3 \
  plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py \
  --stats --pr o/r#1
```

Expected stdout: `No Gemini loop runs recorded yet for this repo. Run the loop once and stats will appear here.`

- [ ] **Step 6: Run the whole suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat(metrics): wire --stats (repo-scoped aggregate, offline)"
```

---

## Task 9: Docs & skill wiring

No code; update docs to match the implemented behavior. Verify by reading the rendered sections.

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md`
- Modify: `README.md`
- Modify: `PRIVACY.md`

- [ ] **Step 1: SKILL.md — add a "Run Metrics" section**

Insert after the "Optional Judge Eval" section. Content:
- What is stored (the record schema summary), where (`~/.config/gh-gemini-review-loop/runs.jsonl`, `GGRL_STATE_DIR`), append-only JSONL.
- The `--record-run --fixed-count <n> --verification <passed|failed|skipped>` contract, plus optional `--verification-details '<json>'`, `--outcome`, `--outcome-reason`. Called once at loop end.
- The "no productivity scoring / no identity recorded" stance (copy the constraint paragraph from the spec).
- Cross-link the `state.json` and judge sections.

- [ ] **Step 2: SKILL.md — workflow step 10 closing sub-step**

Append to step 10:

```markdown
   - After the loop reaches a terminal state, record the run exactly once:
     `python3 "$CLAUDE_PLUGIN_ROOT/.../fetch_gemini_threads.py" --record-run --fixed-count <n> --verification <passed|failed|skipped> [--outcome <state>]`
     then show the printed `[loop] Summary` block to the user.
```

- [ ] **Step 3: SKILL.md — Progress Narration table new row**

Add as the final row of the narration table:

```markdown
| Loop complete / stopped (after DONE/STOP) | `[loop] Summary` block (from `--record-run`) |
```

- [ ] **Step 4: SKILL.md — Variations table new row**

```markdown
| **Local stats** | "show Gemini loop stats" / "loop stats for this repo" / "how's the loop doing here" | `--stats` |
```

- [ ] **Step 5: SKILL.md — guardrail line near Variations**

Add: "Run metrics and `--stats` are local-only — stored under `~/.config/gh-gemini-review-loop/`, never posted to GitHub, and contain no identity."

- [ ] **Step 6: README.md — "Run metrics & local stats" subsection**

After the judge-eval material, add a subsection with the two example outputs (the Feature 1 summary and Feature 2 stats blocks from the spec), the local-only/no-scoring promise, and a note that judge-derived lines appear only when judge mode was on.

- [ ] **Step 7: PRIVACY.md — one sentence**

Add: "Run metrics are stored locally under `~/.config/gh-gemini-review-loop/`, contain no identity (repo and PR number only), and are never transmitted."

- [ ] **Step 8: Verify docs reference real flags**

Run: `grep -n -- "--record-run\|--stats\|runs.jsonl" plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md README.md PRIVACY.md`
Expected: matches in each file; flag names exactly match the argparse definitions from Tasks 7–8.

- [ ] **Step 9: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md README.md PRIVACY.md
git commit -m "docs(metrics): document run summary + local stats, no-scoring stance"
```

---

## Final verification

- [ ] Run the full suite: `python3 -m pytest tests/ -q` — all pass.
- [ ] Lint if the repo uses ruff: `ruff check plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py` — clean.
- [ ] Offline stats smoke (Task 8 Step 5) prints the friendly empty message.

---

## Self-review (completed during planning)

**Spec coverage:** Storage/schema → Tasks 1–3; Feature 1 summary + persistence → Tasks 3,4,6,7; Feature 2 aggregate → Tasks 5,8; judge-off omission → Tasks 4,5; error handling (corrupt line, unwritable log, missing start) → Tasks 2,3,7; no-scoring/no-identity → record carries only repo+pr (Task 3) and docs (Task 9); testing → every code task is TDD; docs wiring → Task 9. All spec sections map to a task.

**Placeholder scan:** No TBD/TODO; every code step has complete code; every test step has real assertions.

**Type consistency:** `build_record` keyword names match what `derive_record_fields` returns (`findings_fetched`, `observed_fixed_count`, `remaining_actionable`, `addressed_by_reply`, `needs_human`) and what `format_run_summary`/`aggregate`/`format_stats` read. `update_run_tracking` takes `(id, path)` tuples and Task 7 passes `(t["id"], t.get("path"))`. `metrics.VALID_OUTCOMES` / `DEFAULT_WINDOW` referenced in argparse are defined in Task 1/3.

**Spec refinement flagged:** `--outcome`/`--outcome-reason` added as optional agent inputs with script-derived defaults (see "Implementation note" above) — the only deviation from the spec's "outcome is derived" wording, called out for visibility.
