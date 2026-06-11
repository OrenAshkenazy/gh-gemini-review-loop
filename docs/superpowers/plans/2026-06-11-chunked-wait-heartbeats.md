# Chunked Wait Heartbeats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the loop's single opaque background Gemini wait with short foreground wait chunks that emit deterministic, script-owned purple heartbeats between chunks.

**Architecture:** A new chunk engine (`run_wait_chunk`) wraps the existing fingerprint/quiet-period logic but returns after `--wait-chunk-seconds` with one of four statuses (`waiting` / `settling` / `ready` / `timed_out`) instead of blocking until `--timeout`. Cross-chunk progress (anchor, start time, check count, settle fingerprint/since) persists in the existing per-PR sticky state under `run["wait"]`. Heartbeat text is a pure `metrics.py` formatter; JSON stdout stays machine-only and a `--wait-heartbeat` formatter command renders the human block from persisted state. Legacy blocking behavior is untouched when `--wait-chunk-seconds` is omitted.

**Tech Stack:** Python 3.9+ stdlib only (same as existing scripts), pytest (`/opt/homebrew/bin/pytest`).

**Spec:** `docs/superpowers/specs/2026-06-11-chunked-wait-heartbeats-design.md`

**Conventions that apply to every task** (from CLAUDE.md and the existing code):
- `from __future__ import annotations` is already present in every touched file; keep subscripted generics (`dict[str, Any]`, `str | None`).
- Test state isolation: `monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))`.
- Run tests with `/opt/homebrew/bin/pytest` (bare `python3 -m pytest` does not work).
- Human `[loop]` blocks are colored via `loop_color.color_loop` only at print time in `main()`, never inside formatters.

---

### Task 1: `metrics.format_wait_heartbeat` formatter

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py` (add after `format_judge_skip`, ~line 211)
- Test: `tests/test_metrics.py` (append new test class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_metrics.py`:

```python
class TestFormatWaitHeartbeat:
    def test_waiting_status(self):
        out = metrics.format_wait_heartbeat(
            "waiting",
            author="gemini-code-assist",
            elapsed_seconds=90,
            checks=2,
            next_wait_seconds=90,
        )
        assert out == (
            "[loop] waiting for gemini-code-assist — 90s elapsed, "
            "2 checks done, next check in 90s"
        )

    def test_waiting_singular_check(self):
        out = metrics.format_wait_heartbeat(
            "waiting",
            author="gemini-code-assist",
            elapsed_seconds=60,
            checks=1,
            next_wait_seconds=90,
        )
        assert "1 check done" in out
        assert "checks" not in out.replace("1 check done", "")

    def test_settling_status(self):
        out = metrics.format_wait_heartbeat(
            "settling",
            author="gemini-code-assist",
            elapsed_seconds=120,
            checks=3,
            next_wait_seconds=30,
            quiet_period_remaining_seconds=30,
        )
        assert out == (
            "[loop] Gemini responded — waiting for review threads to settle, "
            "30s quiet period remaining"
        )

    def test_timed_out_status(self):
        out = metrics.format_wait_heartbeat(
            "timed_out",
            author="gemini-code-assist",
            elapsed_seconds=905,
            checks=11,
        )
        assert out == (
            "[loop] wait timed out after 15m 5s — Gemini did not confirm; "
            "record with --gemini-unconfirmed"
        )

    def test_unknown_status_returns_empty(self):
        assert metrics.format_wait_heartbeat("ready", author="x", elapsed_seconds=1, checks=1) == ""
        assert metrics.format_wait_heartbeat("", author="x", elapsed_seconds=1, checks=1) == ""

    def test_corrupt_counts_clamped(self):
        out = metrics.format_wait_heartbeat(
            "waiting",
            author="gemini-code-assist",
            elapsed_seconds=-5,
            checks="bogus",
            next_wait_seconds=None,
        )
        assert "0s elapsed" in out
        assert "0 checks done" in out
        assert "next check in 0s" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_metrics.py::TestFormatWaitHeartbeat -q`
Expected: FAIL — `AttributeError: module 'metrics' has no attribute 'format_wait_heartbeat'`

- [ ] **Step 3: Implement the formatter**

Add to `metrics.py` directly after `format_judge_skip` (it reuses the existing `_count` and `format_duration` helpers — both already defined in this module):

```python
def format_wait_heartbeat(
    status: str,
    *,
    author: str,
    elapsed_seconds: Any = 0,
    checks: Any = 0,
    next_wait_seconds: Any = 0,
    quiet_period_remaining_seconds: Any = None,
) -> str:
    """Deterministic human heartbeat for one chunked-wait status.

    Returns "" for statuses that have no heartbeat (``ready`` proceeds into a
    fetch; unknown statuses render nothing rather than guessing).
    """
    elapsed = _count(elapsed_seconds)
    checks_n = _count(checks)
    if status == "waiting":
        plural = "check" if checks_n == 1 else "checks"
        return (
            f"[loop] waiting for {author} — {elapsed}s elapsed, "
            f"{checks_n} {plural} done, next check in {_count(next_wait_seconds)}s"
        )
    if status == "settling":
        return (
            "[loop] Gemini responded — waiting for review threads to settle, "
            f"{_count(quiet_period_remaining_seconds)}s quiet period remaining"
        )
    if status == "timed_out":
        return (
            f"[loop] wait timed out after {format_duration(elapsed)} — "
            "Gemini did not confirm; record with --gemini-unconfirmed"
        )
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_metrics.py::TestFormatWaitHeartbeat -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/metrics.py tests/test_metrics.py
git commit -m "feat: add deterministic wait heartbeat formatter"
```

---

### Task 2: Cross-chunk wait state in sticky state

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (add after `clear_run_tracking`, ~line 766)
- Test: `tests/test_fetch_gemini_threads.py` (append new test class)

State shape, stored under the existing per-PR `run` entry (`state["owner/repo#N"]["run"]["wait"]`):

```text
after              ISO anchor (None for cycle 1) — spec name: wait_after
started_at         wall-clock ISO of first chunk   — spec name: wait_started_at
checks             chunk invocation count          — spec name: wait_checks
stable_fingerprint last activity fingerprint       — spec name: wait_stable_fingerprint
stable_since       wall-clock ISO first seen       — spec name: wait_stable_since
last_snapshot      dict — last pending/timed_out result, read by --wait-heartbeat
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetch_gemini_threads.py` (it already imports `fetch_gemini_threads as fgt`, `PullRequest`, and `save_sticky_state` / `load_sticky_state` — reuse those imports; add any missing name to the existing import line):

```python
class TestWaitChunkState:
    PR = PullRequest(owner="o", repo="r", number=5)

    def test_first_chunk_initializes_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        wait = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        assert wait["after"] == "2026-06-11T12:00:00Z"
        assert wait["checks"] == 1
        assert isinstance(wait["started_at"], str) and wait["started_at"]

    def test_second_chunk_same_anchor_accumulates(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        first = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        second = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        assert second["checks"] == 2
        assert second["started_at"] == first["started_at"]

    def test_anchor_change_resets_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        fgt.save_wait_settle(self.PR, "fp-1", "2026-06-11T12:01:00Z")
        wait = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:30:00Z")
        assert wait["after"] == "2026-06-11T12:30:00Z"
        assert wait["checks"] == 1
        assert "stable_fingerprint" not in wait

    def test_settle_persists_across_chunks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        fgt.save_wait_settle(self.PR, "fp-1", "2026-06-11T12:01:00Z")
        wait = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        assert wait["stable_fingerprint"] == "fp-1"
        assert wait["stable_since"] == "2026-06-11T12:01:00Z"

    def test_clear_wait_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        fgt.clear_wait_state(self.PR)
        assert fgt.read_wait_state(self.PR) == {}

    def test_clear_preserves_other_run_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#5": {"run": {"started_at": "2026-06-11T10:00:00Z", "update_seq": 3}}})
        fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        fgt.clear_wait_state(self.PR)
        run = load_sticky_state()["o/r#5"]["run"]
        assert run["update_seq"] == 3
        assert "wait" not in run

    def test_corrupt_state_fails_open(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#5": {"run": {"wait": "not-a-dict"}}})
        wait = fgt.begin_wait_chunk(self.PR, "2026-06-11T12:00:00Z")
        assert wait["checks"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestWaitChunkState -q`
Expected: FAIL — `AttributeError: ... has no attribute 'begin_wait_chunk'`

- [ ] **Step 3: Implement the state helpers**

Add to `fetch_gemini_threads.py` directly after `clear_run_tracking` (~line 766). They follow the exact load/copy/save pattern of `accumulate_fixed_markers` just below them:

```python
def begin_wait_chunk(pr: PullRequest, after_iso: str | None) -> dict[str, Any]:
    """Open one wait chunk: load cross-chunk wait state, applying the reset rule.

    Reset rule (prevents cross-cycle leakage): if the stored anchor differs
    from ``after_iso``, all wait progress (started_at, checks, settle state)
    belongs to a previous cycle's wait and is discarded. ``checks`` counts
    chunk invocations, incremented once per call. Fails open: state I/O
    errors yield a fresh single-chunk state rather than crashing the wait.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    wait = run.get("wait")
    wait = dict(wait) if isinstance(wait, dict) else {}
    if wait.get("after") != after_iso:
        wait = {"after": after_iso}
    if not isinstance(wait.get("started_at"), str) or not wait.get("started_at"):
        wait["started_at"] = metrics.now_iso()
    wait["checks"] = _safe_int(wait.get("checks")) + 1
    run["wait"] = wait
    entry["run"] = run
    state[key] = entry
    try:
        save_sticky_state(state)
    except OSError as exc:
        print(f"warning: could not persist wait state: {exc}", file=sys.stderr)
    return wait


def read_wait_state(pr: PullRequest) -> dict[str, Any]:
    run = read_run_tracking(pr)
    wait = run.get("wait") if isinstance(run, dict) else None
    return dict(wait) if isinstance(wait, dict) else {}


def _update_wait_state(pr: PullRequest, updates: dict[str, Any]) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    wait = run.get("wait")
    wait = dict(wait) if isinstance(wait, dict) else {}
    wait.update(updates)
    run["wait"] = wait
    entry["run"] = run
    state[key] = entry
    try:
        save_sticky_state(state)
    except OSError as exc:
        print(f"warning: could not persist wait state: {exc}", file=sys.stderr)


def save_wait_settle(pr: PullRequest, fingerprint: str, since_iso: str) -> None:
    """Persist the settle phase so a chunk boundary never restarts the quiet period."""
    _update_wait_state(pr, {"stable_fingerprint": fingerprint, "stable_since": since_iso})


def save_wait_snapshot(pr: PullRequest, snapshot: dict[str, Any]) -> None:
    """Persist the last non-ready chunk result for --wait-heartbeat rendering."""
    _update_wait_state(pr, {"last_snapshot": dict(snapshot)})


def clear_wait_state(pr: PullRequest) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("run"), dict) and "wait" in entry["run"]:
        del entry["run"]["wait"]
        try:
            save_sticky_state(state)
        except OSError as exc:
            print(f"warning: could not clear wait state: {exc}", file=sys.stderr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestWaitChunkState -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat: persist chunked-wait state with anchor reset rule"
```

---

### Task 3: Elapsed floor and decay schedule helpers

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (add after `clear_wait_state` from Task 2)
- Test: `tests/test_fetch_gemini_threads.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
class TestWaitElapsedAndDecay:
    def test_elapsed_from_started_at(self):
        wait = {"started_at": "2026-06-11T12:00:00Z"}
        now = datetime.datetime(2026, 6, 11, 12, 2, 30, tzinfo=datetime.timezone.utc)
        assert fgt.wait_elapsed_seconds(wait, None, now=now) == 150

    def test_after_floor_dominates_when_state_lost(self):
        # Fresh started_at (state was wiped) must not restart the budget:
        # the --after anchor bounds total elapsed.
        wait = {"started_at": "2026-06-11T12:09:00Z"}
        now = datetime.datetime(2026, 6, 11, 12, 10, 0, tzinfo=datetime.timezone.utc)
        assert fgt.wait_elapsed_seconds(wait, "2026-06-11T12:00:00Z", now=now) == 600

    def test_missing_started_at_uses_after(self):
        now = datetime.datetime(2026, 6, 11, 12, 5, 0, tzinfo=datetime.timezone.utc)
        assert fgt.wait_elapsed_seconds({}, "2026-06-11T12:00:00Z", now=now) == 300

    def test_no_inputs_returns_zero(self):
        now = datetime.datetime(2026, 6, 11, 12, 5, 0, tzinfo=datetime.timezone.utc)
        assert fgt.wait_elapsed_seconds({}, None, now=now) == 0
        assert fgt.wait_elapsed_seconds({"started_at": "garbage"}, "also-garbage", now=now) == 0

    def test_decay_schedule(self):
        assert fgt.suggested_next_wait_seconds(0) == 60
        assert fgt.suggested_next_wait_seconds(1) == 60
        assert fgt.suggested_next_wait_seconds(2) == 90
        assert fgt.suggested_next_wait_seconds(10) == 90
```

`tests/test_fetch_gemini_threads.py` must import `datetime` at module level if it does not already — check the imports block at the top of the file and add `import datetime` if missing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestWaitElapsedAndDecay -q`
Expected: FAIL — `AttributeError: ... has no attribute 'wait_elapsed_seconds'`

- [ ] **Step 3: Implement the helpers**

```python
WAIT_FIRST_CHUNK_SECONDS = 60
WAIT_LATER_CHUNK_SECONDS = 90


def _parse_iso_utc(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None


def wait_elapsed_seconds(
    wait: dict[str, Any],
    after_iso: str | None,
    now: _dt.datetime | None = None,
) -> int:
    """Total wait elapsed, robust to state loss.

    ``max(now - started_at, now - after)``: if sticky state is corrupted or
    deleted, a fresh started_at cannot silently restart the --timeout budget —
    the --after anchor still bounds the total. Cycle 1 has no anchor and falls
    back to started_at alone (acceptable: the cycle-1 fast path returns on
    first detected activity).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    candidates = []
    for value in (wait.get("started_at"), after_iso):
        parsed = _parse_iso_utc(value)
        if parsed is not None:
            candidates.append((now - parsed).total_seconds())
    return max(0, int(max(candidates))) if candidates else 0


def suggested_next_wait_seconds(checks: int) -> int:
    """Decay schedule: 60s for the first chunk, 90s after.

    Early silence is what feels broken; by the second heartbeat the user knows
    the loop is waiting, so later checks stretch out. All gaps stay far below
    the 5-minute prompt-cache TTL.
    """
    return WAIT_FIRST_CHUNK_SECONDS if checks <= 1 else WAIT_LATER_CHUNK_SECONDS
```

`fetch_gemini_threads.py` does not currently import `datetime` — add `import datetime as _dt` to its stdlib import block (it matches the `_dt` alias `metrics.py` already uses).

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestWaitElapsedAndDecay -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat: add wait elapsed floor and decay schedule helpers"
```

---

### Task 4: `run_wait_chunk` engine

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (add after `suggested_next_wait_seconds`)
- Test: `tests/test_fetch_gemini_threads.py` (append)

The engine reuses `review_activity_fingerprint` and `fetch_threads` (module-level, so tests monkeypatch them on `fgt`). It does NOT modify `wait_for_stable_review` — legacy blocking behavior stays byte-identical.

- [ ] **Step 1: Write the failing tests**

**Anchor warning:** these tests run against the real wall clock, and the Task 3
timeout floor means `elapsed >= now - after`. A fixed past anchor (e.g. a
hardcoded morning timestamp) would push elapsed past `timeout_seconds` and turn
every expected `waiting`/`settling` into `timed_out`. Tests that expect a
non-timeout status MUST use a freshly generated anchor a few seconds in the
past.

```python
class TestRunWaitChunk:
    PR = PullRequest(owner="o", repo="r", number=5)

    @staticmethod
    def _recent_after(seconds_ago: int = 5) -> str:
        return (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _patch(self, monkeypatch, tmp_path, fingerprints, reviews=None):
        """fingerprints: sequence returned by successive fingerprint calls."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fps = iter(fingerprints)
        monkeypatch.setattr(fgt, "fetch_threads", lambda pr: {"reviews": reviews or []})
        monkeypatch.setattr(
            fgt,
            "review_activity_fingerprint",
            lambda pull_request, author, after_iso=None: next(fps),
        )
        monkeypatch.setattr(fgt.time, "sleep", lambda s: None)

    def test_waiting_when_no_activity(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, [None, None, None])
        clock = iter([0.0, 0.0, 1.0, 2.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: next(clock))
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=self._recent_after(), chunk_seconds=60,
        )
        assert result["status"] == "waiting"
        assert result["checks"] == 1
        assert result["next_wait_seconds"] == 60
        assert result["pull_request"] is None

    def test_settling_when_activity_not_yet_stable(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, ["fp-1", "fp-1"])
        clock = iter([0.0, 0.0, 1.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: next(clock))
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=4500,
            after_iso=self._recent_after(), chunk_seconds=60,
        )
        assert result["status"] == "settling"
        assert result["quiet_period_remaining_seconds"] > 0
        # settle state persisted for the next chunk
        wait = fgt.read_wait_state(self.PR)
        assert wait["stable_fingerprint"] == "fp-1"

    def test_settle_survives_chunk_boundary(self, tmp_path, monkeypatch):
        # Chunk 1 sees fp-1 and persists settle state with a stable_since that
        # already satisfies the 45s quiet period. The anchor must be recent
        # (timeout floor) while stable_since is older than quiet_seconds.
        after = self._recent_after(seconds_ago=120)
        stable_since = self._recent_after(seconds_ago=60)
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        fgt.begin_wait_chunk(self.PR, after)
        fgt.save_wait_settle(self.PR, "fp-1", stable_since)
        # Chunk 2 sees the same fingerprint; quiet period (45s) already elapsed
        # relative to the persisted stable_since → ready immediately, without
        # restarting the quiet period.
        self._patch(monkeypatch, tmp_path, ["fp-1"])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: 0.0)
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=after, chunk_seconds=60,
        )
        assert result["status"] == "ready"
        assert result["pull_request"] is not None
        assert fgt.read_wait_state(self.PR) == {}  # cleared on ready

    def test_cycle1_fast_path_no_anchor(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, ["fp-1"])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: 0.0)
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=None, chunk_seconds=60,
        )
        assert result["status"] == "ready"

    def test_timed_out_via_after_floor_with_lost_state(self, tmp_path, monkeypatch):
        # State was never written before; --after is 20 minutes ago, timeout 900s.
        old_after = self._recent_after(seconds_ago=1200)  # 20 minutes ago
        self._patch(monkeypatch, tmp_path, [None])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: 0.0)
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=old_after, chunk_seconds=60,
        )
        assert result["status"] == "timed_out"
        assert result["elapsed_seconds"] >= 1100

    def test_snapshot_persisted_for_heartbeat(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, [None, None])
        clock = iter([0.0, 0.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr(fgt.time, "monotonic", lambda: next(clock))
        result = fgt.run_wait_chunk(
            self.PR, "gemini-code-assist",
            timeout_seconds=900, interval_seconds=1, quiet_seconds=45,
            after_iso=self._recent_after(), chunk_seconds=60,
        )
        snapshot = fgt.read_wait_state(self.PR)["last_snapshot"]
        assert snapshot["status"] == result["status"] == "waiting"
        assert snapshot["author"] == "gemini-code-assist"
```

These tests use plain `import datetime` (matching Task 3's tests) — `datetime.datetime` / `datetime.timedelta` / `datetime.timezone` as written in `_recent_after`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestRunWaitChunk -q`
Expected: FAIL — `AttributeError: ... has no attribute 'run_wait_chunk'`

- [ ] **Step 3: Implement the engine**

```python
def _latest_submitted_after(
    pull_request: dict[str, Any], author: str, after_iso: str | None
) -> str | None:
    """Newest review submittedAt past the anchor, for the settling JSON payload."""
    times = [
        r.get("submittedAt")
        for r in filter_reviews(pull_request, author)
        if isinstance(r.get("submittedAt"), str)
    ]
    if after_iso:
        times = [t for t in times if t > after_iso]
    return max(times) if times else None


def run_wait_chunk(
    pr: PullRequest,
    author: str,
    *,
    timeout_seconds: int,
    interval_seconds: int,
    quiet_seconds: int,
    after_iso: str | None,
    chunk_seconds: int,
) -> dict[str, Any]:
    """One bounded foreground wait chunk; the cross-chunk state machine's step.

    Returns a dict with ``status`` in {waiting, settling, ready, timed_out}.
    ``ready`` carries ``pull_request`` (proceed into the fetch path exactly as
    the legacy wait would); the others carry heartbeat fields and persist a
    snapshot so ``--wait-heartbeat`` can render the human block later.
    The quiet period is measured against the PERSISTED ``stable_since`` so a
    chunk boundary never restarts settling.
    """
    wait = begin_wait_chunk(pr, after_iso)
    chunk_deadline = time.monotonic() + chunk_seconds
    stable_fingerprint = wait.get("stable_fingerprint")
    stable_since = _parse_iso_utc(wait.get("stable_since"))

    while True:
        pull_request = fetch_threads(pr)
        fingerprint = review_activity_fingerprint(pull_request, author, after_iso=after_iso)
        now_dt = _dt.datetime.now(_dt.timezone.utc)
        elapsed = wait_elapsed_seconds(wait, after_iso, now=now_dt)

        if fingerprint is not None and after_iso is None:
            # Cycle 1 fast path: same semantics as the legacy wait.
            clear_wait_state(pr)
            return {"status": "ready", "pull_request": pull_request}

        quiet_remaining: int | None = None
        if fingerprint is not None:
            if fingerprint != stable_fingerprint:
                stable_fingerprint = fingerprint
                stable_since = now_dt
                save_wait_settle(pr, fingerprint, now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
            quiet_elapsed = (now_dt - stable_since).total_seconds() if stable_since else 0.0
            quiet_remaining = max(0, int(quiet_seconds - quiet_elapsed))
            if quiet_remaining <= 0:
                clear_wait_state(pr)
                return {"status": "ready", "pull_request": pull_request}

        if elapsed >= timeout_seconds:
            snapshot = {
                "status": "timed_out",
                "author": author,
                "elapsed_seconds": elapsed,
                "checks": wait["checks"],
            }
            save_wait_snapshot(pr, snapshot)
            return {**snapshot, "pull_request": None}

        if time.monotonic() >= chunk_deadline:
            next_wait = suggested_next_wait_seconds(wait["checks"])
            snapshot: dict[str, Any] = {
                "status": "settling" if fingerprint is not None else "waiting",
                "author": author,
                "elapsed_seconds": elapsed,
                "checks": wait["checks"],
                "next_wait_seconds": next_wait,
            }
            if fingerprint is not None:
                snapshot["quiet_period_remaining_seconds"] = quiet_remaining
                snapshot["next_wait_seconds"] = max(
                    1, min(next_wait, quiet_remaining or next_wait)
                )
                submitted = _latest_submitted_after(pull_request, author, after_iso)
                if submitted:
                    snapshot["submitted_at"] = submitted
            save_wait_snapshot(pr, snapshot)
            return {**snapshot, "pull_request": None}

        time.sleep(min(interval_seconds, max(0.0, chunk_deadline - time.monotonic())))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestRunWaitChunk -q`
Expected: 6 passed

- [ ] **Step 5: Run the full suite to confirm nothing regressed**

Run: `/opt/homebrew/bin/pytest -q`
Expected: all pass (561 pre-existing + new)

- [ ] **Step 6: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat: add bounded wait chunk engine with persisted settling"
```

---

### Task 5: CLI wiring — `--wait-chunk-seconds`

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py`
  - argparse block near `--quiet-period` (~line 1707)
  - the `if args.wait:` call site in `main()` (~line 2089)
- Test: `tests/test_fetch_gemini_threads.py` (append)

- [ ] **Step 1: Write the failing tests**

CLI-level tests drive `fgt.main()` with monkeypatched argv and network, following the existing pattern in `tests/test_json_stdout_discipline.py`:

```python
class TestWaitChunkCli:
    AFTER = "2026-06-11T12:00:00Z"

    def _patch_common(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
        monkeypatch.setattr(
            fgt, "resolve_pr", lambda arg: PullRequest(owner="o", repo="r", number=5)
        )
        monkeypatch.setattr(
            fgt,
            "run_wait_chunk",
            lambda *a, **k: {
                "status": "waiting",
                "author": "gemini-code-assist",
                "elapsed_seconds": 90,
                "checks": 2,
                "next_wait_seconds": 90,
                "pull_request": None,
            },
        )

    def test_pending_markdown_prints_purple_heartbeat(self, tmp_path, monkeypatch, capsys):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--wait", "--after", self.AFTER, "--wait-chunk-seconds", "60"],
        )
        assert fgt.main() == 0
        out = capsys.readouterr().out
        assert "[loop] waiting for gemini-code-assist — 90s elapsed" in out
        assert "\033[95m" in out  # purple

    def test_pending_json_stdout_is_machine_only(self, tmp_path, monkeypatch, capsys):
        self._patch_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--wait", "--after", self.AFTER, "--wait-chunk-seconds", "60",
             "--format", "json"],
        )
        assert fgt.main() == 0
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "[loop]" not in out
        payload = json.loads(out)
        assert payload["wait"]["status"] == "waiting"
        assert payload["wait"]["next_wait_seconds"] == 90
        assert "pull_request" not in payload["wait"]

    def test_no_chunk_flag_uses_legacy_blocking_wait(self, tmp_path, monkeypatch):
        self._patch_common(monkeypatch, tmp_path)
        called = {}

        def fake_legacy(pr, author, timeout_seconds, interval_seconds, quiet_seconds, after_iso=None):
            called["legacy"] = True
            raise RuntimeError("stop here")  # abort main() after the call we care about

        monkeypatch.setattr(fgt, "wait_for_stable_review", fake_legacy)
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
             "--wait", "--after", self.AFTER],
        )
        try:
            fgt.main()
        except RuntimeError:
            pass
        assert called.get("legacy") is True
```

Note for the implementer: `wait_for_stable_review` is called with keyword args in `main()` — keep the fake's signature keyword-compatible (`author=`, `timeout_seconds=`, ...). Check the actual call at ~line 2090 and mirror it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestWaitChunkCli -q`
Expected: FAIL — argparse error `unrecognized arguments: --wait-chunk-seconds`

- [ ] **Step 3: Add the argparse flag**

Next to `--quiet-period` (~line 1707):

```python
    parser.add_argument(
        "--wait-chunk-seconds",
        type=int,
        default=None,
        help=(
            "With --wait, return after at most this many seconds with a "
            "deterministic waiting/settling/timed_out status instead of "
            "blocking until --timeout. --timeout stays the TOTAL wait budget "
            "across chunks. Omit for legacy blocking behavior."
        ),
    )
```

- [ ] **Step 4: Wire the chunk path in `main()`**

Replace the `if args.wait:` block at ~line 2089:

```python
        if args.wait and args.wait_chunk_seconds:
            chunk = run_wait_chunk(
                pr,
                args.author,
                timeout_seconds=args.timeout,
                interval_seconds=args.interval,
                quiet_seconds=args.quiet_period,
                after_iso=args.after,
                chunk_seconds=args.wait_chunk_seconds,
            )
            if chunk["status"] != "ready":
                wait_fields = {
                    k: v
                    for k, v in chunk.items()
                    if k not in ("pull_request", "author") and v is not None
                }
                if args.format == "json":
                    print(json.dumps({"wait": wait_fields}, indent=2, sort_keys=True))
                else:
                    print(
                        color_loop_block(
                            metrics.format_wait_heartbeat(
                                chunk["status"],
                                author=args.author,
                                elapsed_seconds=chunk.get("elapsed_seconds", 0),
                                checks=chunk.get("checks", 0),
                                next_wait_seconds=chunk.get("next_wait_seconds", 0),
                                quiet_period_remaining_seconds=chunk.get(
                                    "quiet_period_remaining_seconds"
                                ),
                            ),
                            enabled=color_enabled,
                        )
                    )
                return 0
            pull_request = chunk["pull_request"]
        elif args.wait:
            pull_request = wait_for_stable_review(
                pr,
                author=args.author,
                timeout_seconds=args.timeout,
                interval_seconds=args.interval,
                quiet_seconds=args.quiet_period,
                after_iso=args.after,
            )
        else:
            pull_request = fetch_threads(pr)
```

(Keep the legacy `wait_for_stable_review` call exactly as it currently is in the file — argument style included; the snippet above mirrors today's call site.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestWaitChunkCli -q`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat: wire --wait-chunk-seconds with status output"
```

---

### Task 6: `--wait-heartbeat` formatter command

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py`
  - argparse block next to `--profile-intro` (~line 1693)
  - formatter-command handling in `main()` right after the `--profile-intro/--planned-verification` block (~line 2017)
- Test: `tests/test_fetch_gemini_threads.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
class TestWaitHeartbeatCommand:
    def test_renders_persisted_snapshot(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
        pr = PullRequest(owner="o", repo="r", number=5)
        monkeypatch.setattr(fgt, "resolve_pr", lambda arg: pr)
        fgt.begin_wait_chunk(pr, "2026-06-11T12:00:00Z")
        fgt.save_wait_snapshot(pr, {
            "status": "settling",
            "author": "gemini-code-assist",
            "elapsed_seconds": 120,
            "checks": 3,
            "next_wait_seconds": 30,
            "quiet_period_remaining_seconds": 30,
        })
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--wait-heartbeat",
             "--pr", "https://github.com/o/r/pull/5"],
        )
        assert fgt.main() == 0
        out = capsys.readouterr().out
        assert "Gemini responded — waiting for review threads to settle" in out
        assert "30s quiet period remaining" in out
        assert "\033[95m" in out

    def test_no_wait_in_progress(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(
            fgt, "resolve_pr", lambda arg: PullRequest(owner="o", repo="r", number=5)
        )
        monkeypatch.setattr(
            sys, "argv",
            ["fetch_gemini_threads.py", "--wait-heartbeat",
             "--pr", "https://github.com/o/r/pull/5"],
        )
        assert fgt.main() == 0
        assert "no Gemini wait in progress" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestWaitHeartbeatCommand -q`
Expected: FAIL — `unrecognized arguments: --wait-heartbeat`

- [ ] **Step 3: Implement flag and handler**

Argparse (next to `--profile-intro`):

```python
    parser.add_argument(
        "--wait-heartbeat",
        action="store_true",
        help=(
            "Print the human heartbeat block for the PR's in-progress chunked "
            "wait (from persisted state) and exit. Use after a --format json "
            "wait chunk so JSON stdout stays machine-only."
        ),
    )
```

Handler in `main()`, placed immediately after the existing `--profile-intro/--planned-verification` early-exit block (~line 2017; it must run before the `--stats` block so it follows the same formatter-command pattern):

```python
    if args.wait_heartbeat:
        pr = resolve_pr(args.pr)
        snapshot = read_wait_state(pr).get("last_snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            print(
                color_loop_block(
                    "[loop] no Gemini wait in progress for this PR.",
                    enabled=color_enabled,
                )
            )
            return 0
        print(
            color_loop_block(
                metrics.format_wait_heartbeat(
                    str(snapshot.get("status", "")),
                    author=str(snapshot.get("author", DEFAULT_AUTHOR)),
                    elapsed_seconds=snapshot.get("elapsed_seconds", 0),
                    checks=snapshot.get("checks", 0),
                    next_wait_seconds=snapshot.get("next_wait_seconds", 0),
                    quiet_period_remaining_seconds=snapshot.get(
                        "quiet_period_remaining_seconds"
                    ),
                )
                or "[loop] no Gemini wait in progress for this PR.",
                enabled=color_enabled,
            )
        )
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_gemini_threads.py::TestWaitHeartbeatCommand -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py tests/test_fetch_gemini_threads.py
git commit -m "feat: add --wait-heartbeat formatter command"
```

---

### Task 7: JSON stdout discipline tests for wait chunks

**Files:**
- Test: `tests/test_json_stdout_discipline.py` (append)

- [ ] **Step 1: Write the tests** (these should pass already if Tasks 5–6 are correct — they are regression locks, not TDD reds)

```python
def test_wait_chunk_json_stdout_is_machine_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
    monkeypatch.setattr(
        fgt, "resolve_pr", lambda arg: PullRequest(owner="o", repo="r", number=5)
    )
    monkeypatch.setattr(
        fgt,
        "run_wait_chunk",
        lambda *a, **k: {
            "status": "settling",
            "author": "gemini-code-assist",
            "elapsed_seconds": 120,
            "checks": 3,
            "next_wait_seconds": 30,
            "quiet_period_remaining_seconds": 30,
            "submitted_at": "2026-06-11T12:04:27Z",
            "pull_request": None,
        },
    )
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
         "--wait", "--after", "2026-06-11T12:00:00Z",
         "--wait-chunk-seconds", "60", "--format", "json"],
    )
    assert fgt.main() == 0
    payload = assert_json_stdout(capsys.readouterr().out)
    assert payload["wait"]["status"] == "settling"
    assert payload["wait"]["submitted_at"] == "2026-06-11T12:04:27Z"
    assert payload["wait"]["quiet_period_remaining_seconds"] == 30


def test_wait_timed_out_json_stdout_is_machine_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        fgt, "resolve_pr", lambda arg: PullRequest(owner="o", repo="r", number=5)
    )
    monkeypatch.setattr(
        fgt,
        "run_wait_chunk",
        lambda *a, **k: {
            "status": "timed_out",
            "author": "gemini-code-assist",
            "elapsed_seconds": 905,
            "checks": 11,
            "pull_request": None,
        },
    )
    monkeypatch.setattr(
        sys, "argv",
        ["fetch_gemini_threads.py", "--pr", "https://github.com/o/r/pull/5",
         "--wait", "--after", "2026-06-11T12:00:00Z",
         "--wait-chunk-seconds", "60", "--format", "json"],
    )
    assert fgt.main() == 0
    payload = assert_json_stdout(capsys.readouterr().out)
    assert payload["wait"]["status"] == "timed_out"
```

- [ ] **Step 2: Run the tests**

Run: `/opt/homebrew/bin/pytest tests/test_json_stdout_discipline.py -q`
Expected: all pass (if either new test fails, fix the Task 5 wiring — JSON mode must never print `[loop]` or ANSI to stdout)

- [ ] **Step 3: Commit**

```bash
git add tests/test_json_stdout_discipline.py
git commit -m "test: lock JSON stdout discipline for wait chunks"
```

---

### Task 8: SKILL.md orchestration update

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md`

- [ ] **Step 1: Update the "Wait" guidance in Script Usage**

In the `## Script Usage` "Useful options" block, extend the two wait examples to their chunked forms:

```bash
# Chunked wait (preferred): return within 60s with a deterministic status
# instead of blocking. Relay the printed heartbeat verbatim, then run the next
# chunk with the suggested --wait-chunk-seconds. Never run the wait in the
# background.
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" \
    --wait --after "$REREVIEW_AT" --wait-chunk-seconds 60

# After a --format json chunk, render the human heartbeat for relay:
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" \
    --wait-heartbeat
```

- [ ] **Step 2: Add the chunked-wait loop rule to the workflow (step 10) and statuses**

Where step 10 currently says "Wait for Gemini after the re-review timestamp before any terminal recording", replace the single `--wait --after` command block with:

```markdown
    - Wait for Gemini using chunked foreground waits — never a background wait:
      ```bash
      python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" \
        --wait \
        --after "$REREVIEW_AT" \
        --wait-chunk-seconds 60
      ```
      Statuses: `waiting` (no response yet), `settling` (Gemini responded,
      quiet period running), `ready` (proceed — the same call returns the
      fetched threads), `timed_out` (total `--timeout` budget exhausted).
      After each `waiting`/`settling` chunk: relay the printed `[loop]`
      heartbeat verbatim (markdown mode) or run `--wait-heartbeat` and relay
      its output (JSON mode), then immediately run the next chunk passing the
      script's `next_wait_seconds` as `--wait-chunk-seconds`. The script owns
      the 60s→90s decay; do not invent intervals.
      On `timed_out`, terminal recording uses `--gemini-unconfirmed`; do not
      guess `clean`, do not blindly mark `capped`, and allow
      `fixed_pending_confirmation`.
```

- [ ] **Step 3: Add the heartbeat row to the Required narration points table**

After the "After final re-review request" row, add:

```markdown
| During any Gemini wait | Run chunked waits (`--wait-chunk-seconds`); after each non-ready chunk relay the script's heartbeat block verbatim, then start the next chunk. Never background the wait; never go silent for more than ~90s. |
```

- [ ] **Step 4: Add the heartbeat to "Script-owned human blocks" list**

In the `### Script-owned human blocks` bullet list, add:

```markdown
- wait heartbeat (`--wait-chunk-seconds` pending output, or `--wait-heartbeat`)
```

- [ ] **Step 5: Run the full suite (SKILL.md has consistency tests in some repos — confirm none broke)**

Run: `/opt/homebrew/bin/pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md
git commit -m "docs: orchestrate chunked foreground waits with heartbeats"
```

---

### Task 9: Final verification

- [ ] **Step 1: Full suite**

Run: `/opt/homebrew/bin/pytest -q`
Expected: 0 failures, total = 561 pre-existing + ~25 new

- [ ] **Step 2: Lint (CI runs ruff)**

Run: `ruff check plugins tests` (or `rtk proxy ruff check plugins tests`)
Expected: no errors. Fix any `E501`/unused-import findings introduced by the new code.

- [ ] **Step 3: Manual smoke — formatter commands need no GitHub**

```bash
S=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts
python3 $S/fetch_gemini_threads.py --wait-heartbeat --pr https://github.com/o/r/pull/5
```

Expected: purple `[loop] no Gemini wait in progress for this PR.`

- [ ] **Step 4: Legacy regression check**

Confirm no test that exercised `--wait` without `--wait-chunk-seconds` changed expectations — `git diff main -- tests/ | grep "wait_for_stable_review"` should show no modified legacy assertions, only additions.

- [ ] **Step 5: Deployment note (do not automate in this plan)**

To live-test before release, copy the changed files into the installed cache per CLAUDE.md:
`fetch_gemini_threads.py`, `metrics.py`, `SKILL.md` →
`~/.claude/plugins/cache/gh-gemini-review-loop/gh-gemini-review-loop/<active-version>/skills/gh-gemini-review-loop/`
