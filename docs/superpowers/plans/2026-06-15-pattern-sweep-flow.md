# Pattern → Sweep → Converge Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cluster Gemini findings by a deterministic pattern signature, sweep sibling instances across the PR's changed files in one cycle, and surface an advisory convergence metric when a swept pattern recurs.

**Architecture:** One new pure module (`cluster_findings.py`) computes pattern signatures, clusters, and recurrence math — no network, no LLM. `metrics.py` gains receipt rendering + a `patterns` block in the terminal record. `fetch_gemini_threads.py` gets thin wiring: per-cycle pattern-signature state tracking (mirroring the existing `seen_finding_fps` machinery), a `--swept-pattern` marker flag (mirroring `--fixed-finding`), and the cluster/convergence output spliced into the existing receipt print path. The sweep itself is an agent step documented in `SKILL.md`.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), stdlib only, pytest. Tests live in `tests/` (import path wired by `tests/conftest.py`). Run via `/opt/homebrew/bin/pytest`.

---

## File Structure

- **Create** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/cluster_findings.py` — pure analysis: `pattern_signature`, `Cluster`, `cluster`, `recurrence_stats`. Self-contained (does not import the heavy `fetch_gemini_threads` module; duplicates two trivial regex helpers).
- **Create** `tests/test_cluster_findings.py` — unit tests for the above.
- **Modify** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py` — add `format_patterns_block`, `format_convergence_line`; add a `patterns` kwarg to `build_record`.
- **Modify** `tests/test_metrics.py` — tests for the two new formatters and the record field.
- **Modify** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` — `track_pattern_signatures`, `prior_pattern_signatures`, `accumulate_swept_patterns`, `read_swept_patterns`; `--swept-pattern` flag; splice cluster + convergence into the receipt print path.
- **Modify** `tests/test_fetch_gemini_threads.py` — tests for the new state functions.
- **Modify** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md` — document the 5-step flow and `--swept-pattern`.

Convention note: all script files start with `from __future__ import annotations`. Subscripted generics (`dict[str, Any]`, `list[str]`) are therefore safe — do not change them to bare `dict`/`list`.

---

## Task 1: `pattern_signature` — deterministic pattern skeleton

**Files:**
- Create: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/cluster_findings.py`
- Test: `tests/test_cluster_findings.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cluster_findings.py`:

```python
from cluster_findings import pattern_signature


def _thread(body: str, path: str = "a.py") -> dict:
    return {"path": path, "comments": [{"body": body}]}


def test_same_issue_different_identifiers_shares_signature():
    # Two type-guard findings on different files / identifiers, same KIND.
    a = _thread(
        "![medium](x.svg) Validate that `source` is a dict before calling "
        "`source.get('files')` on line 204.",
        path="render_demo_ui.py",
    )
    b = _thread(
        "![medium](y.svg) Validate that `provenance` is a dict before calling "
        "`provenance.get('sources')` on line 336.",
        path="render_pr_readiness.py",
    )
    assert pattern_signature(a) == pattern_signature(b)


def test_different_issue_has_different_signature():
    guard = _thread("![medium](x.svg) Validate that `x` is a dict before `.get`.")
    indent = _thread(
        "![high](x.svg) Tab-vs-space: leading tabs are not detected because "
        "`lstrip(' ')` only strips spaces."
    )
    assert pattern_signature(guard) != pattern_signature(indent)


def test_malformed_thread_does_not_raise():
    assert pattern_signature(None) == ""
    assert pattern_signature({}) == ""
    assert pattern_signature({"comments": []}) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_cluster_findings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cluster_findings'`.

- [ ] **Step 3: Write the implementation**

Create `cluster_findings.py`:

```python
#!/usr/bin/env python3
"""Cluster Gemini findings by a deterministic pattern signature.

Pure, stdlib-only, no network. Distinct from finding_fingerprint() in
fetch_gemini_threads.py: that keeps path + text to identify ONE finding;
this strips everything location- and instance-specific to capture the KIND
of suggestion, so two findings of the same kind in different files share a
signature.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Local copies of two trivial patterns so this module stays independent of the
# 2800-line fetch_gemini_threads module.
_SEVERITY_RE = re.compile(r"!\[(critical|high|medium|low)\]", re.IGNORECASE)
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")          # ![alt](url) images
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)       # ```suggestion``` etc.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")               # `identifier`
_LINE_ECHO_RE = re.compile(r"\b(?:lines?|cols?|columns?)\s*\d+", re.IGNORECASE)
_COLON_NUM_RE = re.compile(r":\d+\b")                  # :204
_QUOTED_RE = re.compile(r"(['\"]).*?\1")               # 'literal' / "literal"
_NUM_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")


def _first_body(thread: Any) -> str:
    if not isinstance(thread, dict):
        return ""
    comments = thread.get("comments")
    if isinstance(comments, dict):
        comments = comments.get("nodes")
    if not isinstance(comments, list) or not comments:
        return ""
    first = comments[0]
    body = first.get("body") if isinstance(first, dict) else None
    return body if isinstance(body, str) else ""


def _normalize(body: str) -> str:
    body = _IMG_RE.sub(" ", body)
    body = _FENCED_RE.sub(" ", body)
    body = _INLINE_CODE_RE.sub(" ", body)
    body = _LINE_ECHO_RE.sub(" ", body)
    body = _COLON_NUM_RE.sub(" ", body)
    body = _QUOTED_RE.sub(" ", body)
    body = _NUM_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip().lower()
    return body


def pattern_signature(thread: Any) -> str:
    """Short, stable signature of a finding's KIND. '' for malformed input."""
    normalized = _normalize(_first_body(thread))
    if not normalized:
        return ""
    return hashlib.sha1(normalized[:1000].encode()).hexdigest()[:8]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_cluster_findings.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/cluster_findings.py tests/test_cluster_findings.py
git commit -m "feat: pattern_signature for deterministic finding clustering

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `Cluster` and `cluster()` — group findings by pattern

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/cluster_findings.py`
- Test: `tests/test_cluster_findings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cluster_findings.py`:

```python
from cluster_findings import Cluster, cluster


def _thread_full(body, path, line, sev_alt):
    return {
        "path": path,
        "line": line,
        "comments": [{"body": f"![{sev_alt}](x.svg) {body}"}],
    }


def test_cluster_groups_and_picks_max_severity_and_sorts():
    threads = [
        _thread_full("Validate x is a dict before .get", "render_demo_ui.py", 204, "medium"),
        _thread_full("Validate y is a dict before .get", "render_pr_readiness.py", 336, "medium"),
        _thread_full("Leading tabs not detected by lstrip space", "config_parser.py", 68, "high"),
    ]
    clusters = cluster(threads)
    assert len(clusters) == 2
    # Sorted by severity desc: the HIGH (1 site) comes before the medium (2 sites).
    assert clusters[0].severity == "high"
    assert clusters[0].count == 1
    assert clusters[0].sites == ["config_parser.py:68"]
    assert clusters[1].severity == "medium"
    assert clusters[1].count == 2
    assert "render_demo_ui.py:204" in clusters[1].sites


def test_cluster_ignores_non_dict_members():
    clusters = cluster([None, "nope", {}])
    # Empty-signature threads are dropped, not clustered together.
    assert clusters == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_cluster_findings.py -k cluster -v`
Expected: FAIL with `ImportError: cannot import name 'Cluster'`.

- [ ] **Step 3: Write the implementation**

Append to `cluster_findings.py`:

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Cluster:
    signature: str
    label: str
    severity: str
    sites: list[str]
    count: int


def _severity(thread: Any) -> str:
    body = _first_body(thread)
    match = _SEVERITY_RE.search(body)
    return match.group(1).lower() if match else "unknown"


def _site(thread: dict[str, Any]) -> str:
    path = thread.get("path") or "?"
    line = thread.get("line")
    if line is None:
        line = thread.get("originalLine")
    return f"{path}:{line}" if line is not None else str(path)


def _label(body: str) -> str:
    """Short human title: the normalized prose, truncated. Not polished."""
    norm = _normalize(body)
    return (norm[:60] + "…") if len(norm) > 60 else norm


def cluster(threads: list[Any]) -> list[Cluster]:
    """Group threads by pattern_signature; sort by severity desc then count desc."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for thread in threads:
        sig = pattern_signature(thread)
        if not sig:
            continue
        groups.setdefault(sig, []).append(thread)

    clusters: list[Cluster] = []
    for sig, members in groups.items():
        best = min(members, key=lambda t: _SEVERITY_ORDER[_severity(t)])
        severity = _severity(best)
        clusters.append(
            Cluster(
                signature=sig,
                label=_label(_first_body(best)),
                severity=severity,
                sites=[_site(m) for m in members],
                count=len(members),
            )
        )
    clusters.sort(key=lambda c: (_SEVERITY_ORDER[c.severity], -c.count))
    return clusters
```

Note: add `from dataclasses import dataclass, field` to the existing import block at the top instead of mid-file if you prefer; the engineer may place it with the other imports. `field` is imported for forward-compat but unused now — drop it if your linter complains.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_cluster_findings.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/cluster_findings.py tests/test_cluster_findings.py
git commit -m "feat: Cluster dataclass and cluster() grouping by pattern signature

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `recurrence_stats` — convergence math

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/cluster_findings.py`
- Test: `tests/test_cluster_findings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cluster_findings.py`:

```python
from cluster_findings import recurrence_stats


def test_recurrence_stats_basic():
    current = ["sigA", "sigA", "sigB"]      # 3 findings, 2 distinct
    prior = {"sigA"}                         # sigA seen before
    swept = {"sigA"}                         # sigA was swept last cycle
    stats = recurrence_stats(current, prior_sigs=prior, swept_sigs=swept)
    assert stats["distinct_patterns"] == 2
    assert stats["recurrence_rate"] == 2 / 3   # 2 of 3 findings carry a prior sig
    assert stats["recurred_after_sweep"] == ["sigA"]


def test_recurrence_stats_empty():
    stats = recurrence_stats([], prior_sigs=set(), swept_sigs=set())
    assert stats["distinct_patterns"] == 0
    assert stats["recurrence_rate"] == 0.0
    assert stats["recurred_after_sweep"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_cluster_findings.py -k recurrence -v`
Expected: FAIL with `ImportError: cannot import name 'recurrence_stats'`.

- [ ] **Step 3: Write the implementation**

Append to `cluster_findings.py`:

```python
def recurrence_stats(
    current_sigs: list[str],
    *,
    prior_sigs: set[str],
    swept_sigs: set[str],
) -> dict[str, Any]:
    """Convergence signals for one cycle.

    - distinct_patterns: number of unique signatures this cycle
    - recurrence_rate: fraction of this cycle's findings whose signature was
      seen in a prior cycle (0.0 when there are no findings)
    - recurred_after_sweep: sorted signatures that were swept yet reappeared
    """
    valid = [s for s in current_sigs if s]
    total = len(valid)
    recurred = sum(1 for s in valid if s in prior_sigs)
    recurred_after_sweep = sorted({s for s in valid if s in swept_sigs})
    return {
        "distinct_patterns": len(set(valid)),
        "recurrence_rate": (recurred / total) if total else 0.0,
        "recurred_after_sweep": recurred_after_sweep,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_cluster_findings.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/cluster_findings.py tests/test_cluster_findings.py
git commit -m "feat: recurrence_stats for advisory convergence signals

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: receipt rendering in `metrics.py`

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py` (add after `format_findings_block`, ~line 568)
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_metrics.py`:

```python
from cluster_findings import Cluster
from metrics import format_patterns_block, format_convergence_line


def test_format_patterns_block_orders_and_lists_sites():
    clusters = [
        Cluster(signature="a1b2c3d4", label="tab-vs-space indent detection",
                severity="high", sites=["config_parser.py:68"], count=1),
        Cluster(signature="e5f6a7b8", label="missing isinstance guard",
                severity="medium",
                sites=[f"f{i}.py:{i}" for i in range(8)], count=8),
    ]
    block = format_patterns_block(clusters)
    assert "Patterns (2):" in block
    assert "[high]" in block and "[medium]" in block
    assert "sig: a1b2c3d4" in block
    assert "1 site" in block and "8 sites" in block
    # Long site lists are truncated with a "+N more".
    assert "+" in block and "more" in block


def test_format_patterns_block_empty():
    assert format_patterns_block([]) == ""


def test_format_convergence_line_plain():
    line = format_convergence_line(
        {"distinct_patterns": 2, "recurrence_rate": 0.0, "recurred_after_sweep": []},
        swept_count=1,
    )
    assert "Convergence:" in line
    assert "2 distinct patterns" in line
    assert "Swept 1 pattern" in line
    assert "⚠" not in line


def test_format_convergence_line_recurred_after_sweep_warns():
    line = format_convergence_line(
        {"distinct_patterns": 1, "recurrence_rate": 1.0, "recurred_after_sweep": ["e5f6a7b8"]},
        swept_count=1,
    )
    assert "⚠" in line
    assert "RECURRED after sweep" in line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_metrics.py -k "patterns_block or convergence" -v`
Expected: FAIL with `ImportError: cannot import name 'format_patterns_block'`.

- [ ] **Step 3: Write the implementation**

Add to `metrics.py` immediately after `format_findings_block` (after ~line 568). Note the import at the top of the function body to avoid a hard module-level dependency cycle (metrics is imported very early):

```python
def format_patterns_block(clusters: list[Any]) -> str:
    """Clustered view of the current findings, or '' when none.

    ``clusters`` is a list of cluster_findings.Cluster. Renders above the
    per-finding Findings block: one stanza per pattern with severity, label,
    site count, signature token (for --swept-pattern), and up to 4 sites.
    """
    valid = [c for c in clusters if c is not None]
    if not valid:
        return ""
    lines = [f"Patterns ({len(valid)}):"]
    for c in valid:
        plural = "site" if c.count == 1 else "sites"
        lines.append(f"  [{c.severity}] {c.label} — {c.count} {plural}   (sig: {c.signature})")
        shown = c.sites[:4]
        suffix = f", +{len(c.sites) - 4} more" if len(c.sites) > 4 else ""
        lines.append(f"           {', '.join(shown)}{suffix}")
    return "\n".join(lines)


def format_convergence_line(stats: dict[str, Any], *, swept_count: int) -> str:
    """One-line advisory convergence summary, or '' when no patterns this cycle."""
    distinct = stats.get("distinct_patterns", 0)
    if not distinct:
        return ""
    recurred = stats.get("recurred_after_sweep") or []
    swept_plural = "pattern" if swept_count == 1 else "patterns"
    if recurred:
        names = ", ".join(recurred)
        return (
            f"Convergence: ⚠ pattern(s) {names} RECURRED after sweep. "
            "Sweep missed a variant or Gemini keeps re-flagging."
        )
    return (
        f"Convergence: {distinct} distinct patterns this cycle, "
        f"0 recurred. Swept {swept_count} {swept_plural}."
    )
```

Add `from typing import Any` is already imported in metrics.py (verify at top; it is used throughout). No new import needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_metrics.py -k "patterns_block or convergence" -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py tests/test_metrics.py
git commit -m "feat: format_patterns_block and format_convergence_line receipt rendering

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `patterns` block in the terminal record

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py` (`build_record`, ~line 751–812)
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_metrics.py`:

```python
from metrics import build_record


def _min_record_kwargs():
    return dict(
        repo="o/r", pr=46, provider="gemini-code-assist",
        findings_fetched=3, fixed_count=3, observed_fixed_count=3,
        remaining_actionable=0, needs_human=0, addressed_by_reply=0,
        cycles_used=2, cycle_cap=5, verification="passed",
        verification_details=None, outcome="clean", outcome_reason="",
        started_at=None, finding_paths=[], judge=None,
    )


def test_build_record_includes_patterns_block_when_passed():
    rec = build_record(**_min_record_kwargs(), patterns={
        "distinct_patterns": 4, "max_cluster_size": 14,
        "pattern_recurrence_rate": 0.0, "swept_count": 1,
    })
    assert rec["patterns"]["max_cluster_size"] == 14
    assert rec["patterns"]["swept_count"] == 1


def test_build_record_patterns_defaults_to_empty_dict():
    rec = build_record(**_min_record_kwargs())
    assert rec["patterns"] == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_metrics.py -k "patterns_block_when_passed or patterns_defaults" -v`
Expected: FAIL — `build_record() got an unexpected keyword argument 'patterns'`.

- [ ] **Step 3: Write the implementation**

In `metrics.py`, add a `patterns` parameter to `build_record`. Add to the signature (after `terminal_breakdown: dict[str, Any] | None = None,`, ~line 772):

```python
    patterns: dict[str, Any] | None = None,
```

And add to the returned dict (after the `terminal_breakdown` line, ~line 811):

```python
        "patterns": dict(patterns) if isinstance(patterns, dict) else {},
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/opt/homebrew/bin/pytest tests/test_metrics.py -k "patterns_block_when_passed or patterns_defaults" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py tests/test_metrics.py
git commit -m "feat: additive patterns block in build_record terminal record

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: pattern-signature state tracking + swept markers

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (add near `track_finding_fingerprints` ~line 1126 and `accumulate_fixed_markers` ~line 991)
- Test: `tests/test_fetch_gemini_threads.py`

These mirror the existing `seen_finding_fps` / `accumulate_fixed_markers` machinery exactly, at pattern granularity.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetch_gemini_threads.py` (it already imports the module and uses `GGRL_STATE_DIR` via a tmp fixture — follow the existing pattern in that file for setting the state dir; the snippet below assumes a `pr` helper and monkeypatched state dir like the existing tests):

```python
import fetch_gemini_threads as fgt


def test_track_pattern_signatures_snapshots_prior(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    pr = fgt.PullRequest(owner="o", repo="r", number=46)
    # Cycle 1: no prior.
    out1 = fgt.track_pattern_signatures(pr, {"sigA"})
    assert out1["prior"] == set()
    assert out1["new"] == {"sigA"}
    # Cycle 2: sigA is now prior; sigB is new.
    out2 = fgt.track_pattern_signatures(pr, {"sigA", "sigB"})
    assert out2["prior"] == {"sigA"}
    assert out2["new"] == {"sigB"}
    assert fgt.prior_pattern_signatures(pr) == {"sigA"}


def test_accumulate_and_read_swept_patterns(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    pr = fgt.PullRequest(owner="o", repo="r", number=46)
    fgt.accumulate_swept_patterns(pr, ["sigA"])
    fgt.accumulate_swept_patterns(pr, ["sigB", "sigA"])
    assert fgt.read_swept_patterns(pr) == {"sigA", "sigB"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py -k "pattern_signatures or swept_patterns" -v`
Expected: FAIL with `AttributeError: module 'fetch_gemini_threads' has no attribute 'track_pattern_signatures'`.

- [ ] **Step 3: Write the implementation**

In `fetch_gemini_threads.py`, add directly below `track_finding_fingerprints` / `prior_finding_fingerprints` (~line 1167):

```python
def track_pattern_signatures(pr: PullRequest, current_sigs: set[str]) -> dict[str, set[str]]:
    """Pattern-granularity twin of track_finding_fingerprints.

    Moves the running union into ``prior_seen_pattern_sigs`` BEFORE folding in
    this cycle's signatures, so a later read can ask "was this pattern present
    in a previous cycle?" without the current fetch poisoning the answer.
    Returns ``{"prior": <prior union>, "new": <current − prior>}``. Fails open.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    if not isinstance(entry, dict):
        entry = {}
    run = entry.get("run", {})
    if not isinstance(run, dict):
        run = {}
    prior_val = run.get("seen_pattern_sigs", [])
    prior = {x for x in prior_val if isinstance(x, str)} if isinstance(prior_val, list) else set()
    run["prior_seen_pattern_sigs"] = sorted(prior)
    run["seen_pattern_sigs"] = sorted(prior | current_sigs)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    return {"prior": prior, "new": current_sigs - prior}


def prior_pattern_signatures(pr: PullRequest) -> set[str]:
    """Pattern signatures seen in cycles before the current one. Empty on cycle 1."""
    state = load_sticky_state()
    entry = state.get(_state_key(pr), {})
    run = entry.get("run", {}) if isinstance(entry, dict) else {}
    value = run.get("prior_seen_pattern_sigs", []) if isinstance(run, dict) else []
    return {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()
```

And add directly below `accumulate_fixed_markers` / `read_fixed_markers` (~line 1042):

```python
def accumulate_swept_patterns(pr: PullRequest, signatures: list[str]) -> None:
    """Union agent-supplied --swept-pattern signatures into run tracking.

    Twin of accumulate_fixed_markers. A pattern the agent reports as swept this
    cycle is matched against later cycles' findings to flag recurrence.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    val = run.get("swept_pattern_sigs")
    sigs = {x for x in val if isinstance(x, str)} if isinstance(val, list) else set()
    for s in signatures or []:
        if s:
            sigs.add(s)
    run["swept_pattern_sigs"] = sorted(sigs)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)


def read_swept_patterns(pr: PullRequest) -> set[str]:
    """Return the accumulated swept pattern signatures, or empty set."""
    run = read_run_tracking(pr)
    val = run.get("swept_pattern_sigs", []) if isinstance(run, dict) else []
    return {x for x in val if isinstance(x, str)} if isinstance(val, list) else set()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py -k "pattern_signatures or swept_patterns" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat: pattern-signature state tracking and swept-pattern markers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `--swept-pattern` CLI flag

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (arg parser ~line 2140, and the marker-accumulation block ~line 2356)

- [ ] **Step 1: Add the argument**

After the existing `--fixed-finding` argument block (~line 2140–2148), add:

```python
    parser.add_argument(
        "--swept-pattern",
        action="append",
        default=[],
        metavar="SIG",
        help="Pattern signature (from the Patterns receipt 'sig:' token) the agent "
             "swept across changed files this cycle. Repeatable. Accumulates for "
             "the convergence advisory.",
    )
```

- [ ] **Step 2: Wire accumulation**

Find the existing block that accumulates fixed markers (~line 2356):

```python
        if args.fixed_finding or args.fixed_path:
            accumulate_fixed_markers(
                pr,
                fingerprints=args.fixed_finding,
                ...
            )
```

Immediately after that block, add:

```python
        if args.swept_pattern:
            try:
                accumulate_swept_patterns(pr, args.swept_pattern)
            except OSError as exc:
                print(f"warning: could not persist swept patterns: {exc}", file=sys.stderr)
```

- [ ] **Step 3: Verify the parser accepts it**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py -v`
Expected: PASS (all existing tests still pass; the parser builds without error).

Additionally smoke-test the flag parses:

Run: `python3 plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --help | grep -A2 swept-pattern`
Expected: the `--swept-pattern` help text prints.

- [ ] **Step 4: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py
git commit -m "feat: --swept-pattern CLI flag accumulates swept signatures

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: splice clusters + convergence into the receipt print path

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (the cycle-summary/record-run print block, ~line 2737–2754; the `import` block near line 26; the per-cycle tracking near line 2498; `build_record` call site)

This is the integration task — wiring the pure functions into the existing output. No new pure logic.

- [ ] **Step 1: Import the module**

Near the existing sibling imports (~line 26–27, beside `import metrics`):

```python
import cluster_findings  # noqa: E402 — sibling module, pure/stdlib-only
```

- [ ] **Step 2: Track pattern signatures on the real fetch (mirror line 2498)**

Find the per-cycle finding-fingerprint tracking call (~line 2495–2499):

```python
            # ... track_finding_fingerprints(...) once per cycle ...
            track_finding_fingerprints(
                pr, {finding_fingerprint(t) for t in threads if isinstance(t, dict)}
            )
```

Immediately after it, add the pattern-signature twin:

```python
            track_pattern_signatures(
                pr,
                {cluster_findings.pattern_signature(t) for t in threads if isinstance(t, dict)}
                - {""},
            )
```

- [ ] **Step 3: Compute clusters + convergence once, before `build_record`**

The single `build_record(...)` call is at line 2676; `threads` is already in scope above it. Insert this computation **immediately before** `record = metrics.build_record(` (line 2676, just after the `verification_details` block ends at ~line 2675) so both the record (Step 4) and the print block (Step 5) reuse it — compute once, use twice (DRY):

```python
            clusters = cluster_findings.cluster(
                [t for t in threads if isinstance(t, dict)]
            )
            # One signature entry per finding (repeat per cluster member) so the
            # recurrence rate is over findings, not distinct patterns.
            current_sigs = [c.signature for c in clusters for _ in range(c.count)]
            swept_sigs = read_swept_patterns(pr)
            conv_stats = cluster_findings.recurrence_stats(
                current_sigs,
                prior_sigs=prior_pattern_signatures(pr),
                swept_sigs=swept_sigs,
            )
```

Note: `prior_pattern_signatures(pr)` reflects cycles *before* this one because `track_pattern_signatures` (Step 2) snapshotted the prior union before folding in the current cycle — mirroring how `prior_finding_fingerprints` is read at line 2740.

- [ ] **Step 4: Attach the `patterns` block to the terminal record**

Add a `patterns=` kwarg to the `build_record(...)` call (line 2676–2693), e.g. right before `**derived,` (line 2692):

```python
                patterns={
                    "distinct_patterns": conv_stats["distinct_patterns"],
                    "max_cluster_size": max((c.count for c in clusters), default=0),
                    "pattern_recurrence_rate": round(conv_stats["recurrence_rate"], 3),
                    "swept_count": len(swept_sigs),
                } if clusters else None,
```

- [ ] **Step 5: Render Patterns + Convergence in the print block**

In the receipt print block (~line 2737), find where `suite_block` is printed:

```python
                suite_block = metrics.format_suite_block(verification_details)
                if suite_block:
                    print(suite_block)
```

Immediately after it, print the Patterns block and Convergence line (above the existing Findings block, per the spec), reusing `clusters` / `conv_stats` / `swept_sigs` from Step 3:

```python
                patterns_block = metrics.format_patterns_block(clusters)
                if patterns_block:
                    print(color_loop_block(patterns_block, enabled=color_enabled))
                convergence_line = metrics.format_convergence_line(
                    conv_stats, swept_count=len(swept_sigs)
                )
                if convergence_line:
                    print(color_loop_block(convergence_line, enabled=color_enabled))
```

- [ ] **Step 6: Run the full suite**

Run: `/opt/homebrew/bin/pytest -q`
Expected: PASS — all existing tests plus the new ones. No regressions.

- [ ] **Step 7: Manual smoke (read-only) against the recorded run**

Run: `python3 plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --pr https://github.com/OrenAshkenazy/gh-gemini-review-loop/pull/46 --cycle-summary --fixed-count 0 --verification skipped`
Expected: the `[loop] Cycle receipt` now includes a `Patterns (N):` section and a `Convergence:` line above the `Findings (N):` list. (Network call; safe/read-only — does not push or post.)

- [ ] **Step 8: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py
git commit -m "feat: wire pattern clustering and convergence into the cycle receipt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: document the flow in `SKILL.md`

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md`

- [ ] **Step 1: Add a "Pattern → Sweep" flow section**

Add a new section after the Severity Ordering section (~line 41–44). Document the 5-step cycle and the sweep step explicitly:

```markdown
## Pattern → Sweep → Converge

Gemini is an LLM reviewer: when it flags a code pattern, fixing only the flagged
sites teaches it to flag *more* instances of the same pattern in other changed
files next cycle. To collapse that expansion into one cycle, each cycle runs:

**Finding → Pattern → Sweep → Verify → Re-review**

1. **Cluster.** The cycle receipt's `Patterns (N):` section groups findings by a
   deterministic pattern signature. Reason about patterns, not a flat finding list.
2. **Sweep (report-then-go).** For each multi-site pattern (`count >= 2`), grep the
   PR's **changed files** for sibling instances of the same shape — including ones
   Gemini has not flagged yet — using the cluster's example sites as the template.
   Print a short sweep report (which extra sites, why), then fix the whole cluster
   plus the swept siblings in this cycle. Do not block on approval, but never edit
   unflagged code silently — the report must appear first.
3. **Mark.** Pass `--swept-pattern <sig>` (the `sig:` token from the Patterns
   receipt) for each pattern you swept, alongside the usual `--fixed-finding`
   markers, so the convergence advisory can detect recurrence.
4. **Verify** (the repo profile) and **re-review** as usual.

The receipt's `Convergence:` line is advisory only. When a swept pattern reappears
("⚠ … RECURRED after sweep"), the sweep missed a variant or Gemini keeps
re-flagging — decide whether to refine the sweep, stop, or continue. It never
changes control flow; the re-review cap remains the only hard stop.

Sweep scope is **changed files only** — that is both safe (blast radius = the PR's
own diff) and sufficient (Gemini only reviews changed files).
```

- [ ] **Step 2: Add `--swept-pattern` to the flag reference**

Wherever `--fixed-finding` is documented in SKILL.md (search `--fixed-finding`), add a sibling line for `--swept-pattern <sig>` describing it as the pattern-level swept marker.

- [ ] **Step 3: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md
git commit -m "docs: document the Pattern → Sweep → Converge cycle flow

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the full test suite**

Run: `/opt/homebrew/bin/pytest -q`
Expected: all green, including the new `test_cluster_findings.py` and the added cases in `test_metrics.py` / `test_fetch_gemini_threads.py`.

- [ ] **Confirm no JSON-stdout discipline regressions**

Run: `/opt/homebrew/bin/pytest tests/test_json_stdout_discipline.py -v`
Expected: PASS — the new prints go to the human receipt path, not the `--format json` path, so JSON-mode stdout stays clean.

- [ ] **Open the PR** (only when the user asks)

```bash
git push -u origin feat/pattern-sweep-flow
gh pr create --title "Pattern → Sweep → Converge: collapse Gemini's pattern expansion" --body "<summary + link to spec>"
```
