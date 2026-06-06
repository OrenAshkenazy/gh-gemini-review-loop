# Verification Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the review loop a per-repo, code-derived verification profile (detected, confirmed/customized/skipped, persisted) that it runs every verify step with a `required`-check gate.

**Architecture:** Three layers. `detect_profile.py` (new) does deterministic stack detection. `judge.py` (extended) persists profiles in `preferences.json` keyed by `owner/repo`, schema v2. `run_profile.py` (new) executes a profile's checks with cwd/timeout and computes the gate. `SKILL.md` wires detection-before-first-fix and verify-step usage; the agent reconciles detected commands against repo docs before persisting.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), stdlib only (`json`, `subprocess`, `shlex`, `dataclasses`, `pathlib`). Tests with pytest. Run pytest via `/opt/homebrew/bin/pytest` (or a venv); `python3 -m pytest` does not work in this repo.

**Spec:** `docs/superpowers/specs/2026-06-04-verification-profile-design.md`

**Conventions (read before starting):**
- Scripts live in `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/`. Tests live in `tests/` and import script modules directly (e.g. `from judge import ...`) — `tests/conftest.py` injects the scripts dir onto `sys.path`.
- Isolate prefs in tests with `monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))`. `judge.prefs_path()` reads that env var.
- Every script starts with `from __future__ import annotations`. Do NOT revert subscripted generics to bare `dict`/`list`.
- All pytest commands below use `pytest`; substitute `/opt/homebrew/bin/pytest` if that is how pytest is installed for you.

---

### Task 1: Schema v2 — thread `profiles` through prefs load/save

`judge.load_preferences()` returns a freshly-built dict that **drops unknown keys**, and `save_preferences()` rebuilds the dict from scratch. A naive `profiles` field would be silently lost on the next `save_preferences()` call. This task makes `profiles` a first-class, preserved key and bumps the schema.

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/judge.py` (`PREFS_SCHEMA_VERSION`, `_default_prefs`, `load_preferences`, `save_preferences`)
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (`_FALLBACK_PREFS_DEFAULTS`, `load_preferences_with_fallback`)
- Test: `tests/test_judge.py` (add to `TestPreferences`)

- [ ] **Step 1: Write failing tests for profile preservation**

Add to `tests/test_judge.py` inside `class TestPreferences`:

```python
def test_default_prefs_include_empty_profiles(self, tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    prefs = load_preferences()
    assert prefs["profiles"] == {}
    assert prefs["schema_version"] == 2

def test_save_preferences_preserves_profiles(self, tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({
        "schema_version": 2,
        "judge_mode": "off",
        "profiles": {"o/r": {"source": "confirmed", "checks": []}},
    }))
    # Saving an unrelated judge setting must not wipe profiles.
    save_preferences("on_complete")
    saved = json.loads(path.read_text())
    assert saved["profiles"] == {"o/r": {"source": "confirmed", "checks": []}}

def test_load_coerces_non_dict_profiles_to_empty(self, tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    (tmp_path / "preferences.json").write_text(json.dumps({
        "schema_version": 2, "judge_mode": "off", "profiles": "garbage",
    }))
    assert load_preferences()["profiles"] == {}
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `pytest tests/test_judge.py::TestPreferences -v`
Expected: 3 failures — `KeyError: 'profiles'` / `schema_version == 1`.

- [ ] **Step 3: Implement the changes in `judge.py`**

Change the constant near the top of `judge.py`:

```python
PREFS_SCHEMA_VERSION = 2
```

In `_default_prefs()`, add the `profiles` key:

```python
def _default_prefs() -> dict[str, t.Any]:
    return {
        "schema_version": PREFS_SCHEMA_VERSION,
        "judge_mode": "off",
        "judge_model": DEFAULT_MODEL,
        "judge_tip_shown": False,
        "max_rereview_requests": DEFAULT_MAX_REREVIEW_REQUESTS,
        "profiles": {},
        "set_at": "",
    }
```

In `load_preferences()`, in the returned dict, add a coerced `profiles` entry (place it next to the other keys):

```python
    raw_profiles = data.get("profiles")
    profiles = raw_profiles if isinstance(raw_profiles, dict) else {}
    return {
        "schema_version": data.get("schema_version", PREFS_SCHEMA_VERSION),
        "judge_mode": mode,
        "judge_model": data.get("judge_model") or DEFAULT_MODEL,
        "judge_tip_shown": bool(data.get("judge_tip_shown", False)),
        "max_rereview_requests": max_rereview_requests,
        "profiles": profiles,
        "set_at": data.get("set_at") or "",
    }
```

In `save_preferences()`, preserve existing profiles in the rebuilt dict (add the one line):

```python
    prefs = {
        "schema_version": PREFS_SCHEMA_VERSION,
        "judge_mode": judge_mode,
        "judge_model": judge_model or DEFAULT_MODEL,
        "judge_tip_shown": existing.get("judge_tip_shown", False),
        "max_rereview_requests": existing.get(
            "max_rereview_requests", DEFAULT_MAX_REREVIEW_REQUESTS
        ),
        "profiles": existing.get("profiles", {}),
        "set_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
```

- [ ] **Step 4: Update the fallback loader in `fetch_gemini_threads.py`**

In `_FALLBACK_PREFS_DEFAULTS`, bump the version and add `profiles`:

```python
_FALLBACK_PREFS_DEFAULTS: dict[str, Any] = {
    "schema_version": 2,
    "judge_mode": "off",
    "judge_model": "gpt-4o-mini",
    "judge_tip_shown": False,
    "max_rereview_requests": DEFAULT_REREVIEW_LIMIT,
    "profiles": {},
    "set_at": "",
}
```

The final `return {**_FALLBACK_PREFS_DEFAULTS, **data}` already carries a saved `profiles` through, so no further change is needed there.

- [ ] **Step 5: Run the tests, verify they pass**

Run: `pytest tests/test_judge.py::TestPreferences -v`
Expected: PASS (including the 3 new tests).

- [ ] **Step 6: Run the full suite to check for migration fallout**

Run: `pytest tests/ -q`
Expected: PASS. If a test hard-codes `schema_version == 1`, update it to `2` — the schema legitimately changed.

- [ ] **Step 7: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/judge.py \
        plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py \
        tests/test_judge.py
git commit -m "feat(prefs): schema v2 with preserved per-repo profiles"
```

---

### Task 2: Profile persistence helpers — `get_profile` / `save_profile`

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/judge.py`
- Test: `tests/test_judge.py` (new class `TestProfiles`)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_judge.py` (and add `get_profile, save_profile, PROFILE_SOURCES` to the `from judge import (...)` block at the top of the file):

```python
class TestProfiles:
    def test_get_profile_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert get_profile("o/r") is None

    def test_save_and_get_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        checks = [{"name": "tests", "command": "pytest", "required": True}]
        save_profile("o/r", source="confirmed", checks=checks,
                     detected_stack="python")
        prof = get_profile("o/r")
        assert prof["source"] == "confirmed"
        assert prof["detected_stack"] == "python"
        assert prof["checks"] == checks
        assert prof["working_directory"] == "."
        assert prof["timeout_seconds"] == 300
        assert prof["updated_at"]  # non-empty ISO timestamp

    def test_save_skipped_omits_checks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_profile("o/r", source="skipped")
        prof = get_profile("o/r")
        assert prof["source"] == "skipped"
        assert "checks" not in prof

    def test_save_invalid_source_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        with pytest.raises(ValueError):
            save_profile("o/r", source="bogus", checks=[])

    def test_save_profile_preserves_judge_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_preferences("on_complete")
        save_profile("o/r", source="confirmed", checks=[])
        assert load_preferences()["judge_mode"] == "on_complete"

    def test_two_repos_coexist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_profile("o/a", source="confirmed", checks=[])
        save_profile("o/b", source="skipped")
        assert get_profile("o/a")["source"] == "confirmed"
        assert get_profile("o/b")["source"] == "skipped"
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `pytest tests/test_judge.py::TestProfiles -v`
Expected: ImportError / `NameError` on `get_profile` / `save_profile` / `PROFILE_SOURCES`.

- [ ] **Step 3: Implement the helpers in `judge.py`**

Add near the other prefs helpers (after `mark_tip_shown`):

```python
PROFILE_SOURCES = frozenset({"confirmed", "customized", "skipped"})


def get_profile(repo: str) -> dict[str, t.Any] | None:
    """Return the saved verification profile for ``repo`` (``owner/repo``), or None."""
    return load_preferences().get("profiles", {}).get(repo)


def save_profile(
    repo: str,
    *,
    source: str,
    checks: list[dict[str, t.Any]] | None = None,
    detected_stack: str | None = None,
    working_directory: str = ".",
    timeout_seconds: int = 300,
) -> dict[str, t.Any]:
    """Persist a per-repo verification profile, preserving all other prefs.

    ``source`` must be one of PROFILE_SOURCES. A ``skipped`` profile carries no
    ``checks`` (the loop falls back to ad-hoc verification but does not re-prompt).
    Returns the saved profile dict.
    """
    if source not in PROFILE_SOURCES:
        raise ValueError(
            f"source must be one of {sorted(PROFILE_SOURCES)}; got {source!r}."
        )
    prefs = load_preferences()
    profile: dict[str, t.Any] = {
        "source": source,
        "updated_at": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    if detected_stack is not None:
        profile["detected_stack"] = detected_stack
    if source != "skipped":
        profile["checks"] = checks or []
        profile["working_directory"] = working_directory
        profile["timeout_seconds"] = timeout_seconds
    profiles = dict(prefs.get("profiles", {}))
    profiles[repo] = profile
    prefs["profiles"] = profiles
    _write_prefs(prefs_path(), prefs)
    return profile
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `pytest tests/test_judge.py::TestProfiles -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/judge.py tests/test_judge.py
git commit -m "feat(prefs): get_profile/save_profile with source lifecycle"
```

---

### Task 3: `detect_profile.py` — deterministic stack detection

**Files:**
- Create: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_profile.py`
- Test: `tests/test_detect_profile.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_detect_profile.py`:

```python
from __future__ import annotations

import json

from detect_profile import detect


def _names(result):
    return [c["name"] for c in result["candidate_checks"]]


def test_python_pyproject_with_optional_tools(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['ruff', 'mypy']\n"
    )
    (tmp_path / "tests").mkdir()
    result = detect(tmp_path)
    assert result["stack"] == "python"
    assert result["confidence"] == "high"
    assert "pyproject.toml" in result["reasons"]
    assert _names(result) == ["tests", "lint", "typecheck"]
    tests_check = result["candidate_checks"][0]
    assert tests_check["command"] == "pytest"
    assert tests_check["required"] is True
    assert result["candidate_checks"][2]["required"] is False  # typecheck optional


def test_python_without_optional_tools_only_pytest(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    result = detect(tmp_path)
    assert result["stack"] == "python"
    assert _names(result) == ["tests"]


def test_node_only_emits_existing_scripts(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": "jest", "lint": "eslint ."}
    }))
    result = detect(tmp_path)
    assert result["stack"] == "node"
    assert _names(result) == ["tests", "lint"]  # no typecheck script -> absent
    assert result["candidate_checks"][0]["command"] == "npm test"
    assert result["candidate_checks"][1]["command"] == "npm run lint"


def test_rust_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    result = detect(tmp_path)
    assert result["stack"] == "rust"
    assert result["candidate_checks"][0]["command"] == "cargo test"
    assert result["candidate_checks"][1]["required"] is False  # clippy optional


def test_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    result = detect(tmp_path)
    assert result["stack"] == "go"
    assert result["candidate_checks"][0]["command"] == "go test ./..."


def test_unknown_stack_low_confidence_no_checks(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n")
    result = detect(tmp_path)
    assert result["stack"] == "unknown"
    assert result["confidence"] == "low"
    assert result["candidate_checks"] == []
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `pytest tests/test_detect_profile.py -v`
Expected: `ModuleNotFoundError: No module named 'detect_profile'`.

- [ ] **Step 3: Implement `detect_profile.py`**

Create the file:

```python
#!/usr/bin/env python3
"""Deterministic verification-stack detection for gh-gemini-review-loop.

Pure function of the filesystem: inspects marker files in a repo root and
emits a transient JSON candidate profile. Does NOT read or write preferences;
persistence and prose-reconciliation happen in the agent/judge layers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _check(name: str, command: str, required: bool) -> dict[str, Any]:
    return {"name": name, "command": command, "required": required}


def _detect_python(root: Path) -> dict[str, Any]:
    reasons: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        reasons.append("pyproject.toml")
    if (root / "setup.py").exists():
        reasons.append("setup.py")
    if (root / "tests").is_dir():
        reasons.append("tests/")
    checks = [_check("tests", "pytest", True)]
    deps_text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
    if "ruff" in deps_text:
        checks.append(_check("lint", "ruff check .", True))
    if "mypy" in deps_text:
        checks.append(_check("typecheck", "mypy .", False))
    return {
        "stack": "python",
        "confidence": "high" if pyproject.exists() else "medium",
        "reasons": reasons,
        "candidate_checks": checks,
    }


def _detect_node(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    mapping = [
        ("test", "tests", "npm test", True),
        ("lint", "lint", "npm run lint", True),
        ("typecheck", "typecheck", "npm run typecheck", False),
    ]
    checks = [
        _check(name, cmd, required)
        for script_key, name, cmd, required in mapping
        if script_key in scripts
    ]
    return {
        "stack": "node",
        "confidence": "high",
        "reasons": ["package.json"],
        "candidate_checks": checks,
    }


def _detect_rust(root: Path) -> dict[str, Any]:
    return {
        "stack": "rust",
        "confidence": "high",
        "reasons": ["Cargo.toml"],
        "candidate_checks": [
            _check("tests", "cargo test", True),
            _check("lint", "cargo clippy", False),
        ],
    }


def _detect_go(root: Path) -> dict[str, Any]:
    return {
        "stack": "go",
        "confidence": "high",
        "reasons": ["go.mod"],
        "candidate_checks": [
            _check("tests", "go test ./...", True),
            _check("vet", "go vet ./...", False),
        ],
    }


def detect(repo_root: Path | str) -> dict[str, Any]:
    """Return {stack, confidence, reasons, candidate_checks} for ``repo_root``.

    Detection order is fixed: python, node, rust, go. The first matching marker
    wins. Unknown stacks return a low-confidence empty candidate so the caller
    falls back to ad-hoc verification.
    """
    root = Path(repo_root)
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (
        root / "tests"
    ).is_dir():
        return _detect_python(root)
    if (root / "package.json").exists():
        return _detect_node(root)
    if (root / "Cargo.toml").exists():
        return _detect_rust(root)
    if (root / "go.mod").exists():
        return _detect_go(root)
    return {
        "stack": "unknown",
        "confidence": "low",
        "reasons": [],
        "candidate_checks": [],
    }


def main(argv: list[str]) -> int:
    root = argv[1] if len(argv) > 1 else "."
    print(json.dumps(detect(root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `pytest tests/test_detect_profile.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Smoke-test the CLI against this repo**

Run: `python3 plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_profile.py .`
Expected: JSON with `"stack": "python"` and a `tests` check (this repo has `tests/`).

- [ ] **Step 6: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_profile.py \
        tests/test_detect_profile.py
git commit -m "feat: detect_profile.py deterministic stack detection"
```

---

### Task 4: `run_profile.py` — execute checks with cwd/timeout and the required-gate

**Files:**
- Create: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/run_profile.py`
- Test: `tests/test_run_profile.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_run_profile.py`:

```python
from __future__ import annotations

from run_profile import run_profile


def _profile(checks, working_directory=".", timeout_seconds=300):
    return {
        "checks": checks,
        "working_directory": working_directory,
        "timeout_seconds": timeout_seconds,
    }


def test_all_required_pass_gate_passes(tmp_path):
    prof = _profile([
        {"name": "ok", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"
    assert result.failed_required == []
    assert result.checks[0].status == "passed"


def test_required_failure_fails_gate(tmp_path):
    prof = _profile([
        {"name": "boom", "command": "python3 -c \"import sys; sys.exit(1)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "failed"
    assert result.failed_required == ["boom"]


def test_optional_failure_does_not_fail_gate(tmp_path):
    prof = _profile([
        {"name": "tests", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
        {"name": "types", "command": "python3 -c \"import sys; sys.exit(1)\"",
         "required": False},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"
    assert result.failed_required == []
    statuses = {c.name: c.status for c in result.checks}
    assert statuses == {"tests": "passed", "types": "failed"}


def test_required_timeout_fails_gate(tmp_path):
    prof = _profile(
        [{"name": "slow", "command": "python3 -c \"import time; time.sleep(5)\"",
          "required": True}],
        timeout_seconds=1,
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "failed"
    assert result.checks[0].status == "timeout"
    assert result.failed_required == ["slow"]


def test_working_directory_is_honored(tmp_path):
    sub = tmp_path / "backend"
    sub.mkdir()
    (sub / "marker.txt").write_text("x")
    prof = _profile(
        [{"name": "find", "command": "python3 -c \"import os,sys; sys.exit(0 "
          "if os.path.exists('marker.txt') else 1)\"", "required": True}],
        working_directory="backend",
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"


def test_to_details_dict_is_json_serializable(tmp_path):
    import json
    prof = _profile([
        {"name": "ok", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    json.dumps(result.to_details())  # must not raise
    assert result.to_details()["verification"] == "passed"
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `pytest tests/test_run_profile.py -v`
Expected: `ModuleNotFoundError: No module named 'run_profile'`.

- [ ] **Step 3: Implement `run_profile.py`**

Create the file:

```python
#!/usr/bin/env python3
"""Execute a verification profile's checks and compute the required-gate.

Each check ``command`` is split with ``shlex`` and run WITHOUT a shell, with
``cwd`` set to the profile's ``working_directory`` and a wall-clock timeout.
The gate fails iff any ``required`` check fails or times out; non-required
failures are recorded but non-gating.
"""
from __future__ import annotations

import dataclasses
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class CheckResult:
    name: str
    command: str
    required: bool
    status: str  # "passed" | "failed" | "timeout"
    returncode: int | None
    duration_s: float


@dataclasses.dataclass
class ProfileRunResult:
    verification: str  # "passed" | "failed"
    checks: list[CheckResult]
    failed_required: list[str]

    def to_details(self) -> dict[str, Any]:
        """JSON-serializable shape for --verification-details."""
        return {
            "verification": self.verification,
            "failed_required": self.failed_required,
            "checks": [dataclasses.asdict(c) for c in self.checks],
        }


def _run_one(check: dict[str, Any], cwd: Path, timeout: int) -> CheckResult:
    name = check["name"]
    command = check["command"]
    required = bool(check.get("required", True))
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - command is user-confirmed, no shell
            shlex.split(command),
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name, command, required, "timeout", None,
                           time.monotonic() - start)
    except (FileNotFoundError, OSError):
        return CheckResult(name, command, required, "failed", None,
                           time.monotonic() - start)
    status = "passed" if proc.returncode == 0 else "failed"
    return CheckResult(name, command, required, status, proc.returncode,
                       time.monotonic() - start)


def run_profile(profile: dict[str, Any], repo_root: Path | str) -> ProfileRunResult:
    """Run all checks in ``profile`` rooted at ``repo_root``; compute the gate."""
    root = Path(repo_root)
    cwd = root / profile.get("working_directory", ".")
    timeout = int(profile.get("timeout_seconds", 300))
    results = [_run_one(c, cwd, timeout) for c in profile.get("checks", [])]
    failed_required = [
        c.name for c in results if c.required and c.status != "passed"
    ]
    verification = "failed" if failed_required else "passed"
    return ProfileRunResult(verification, results, failed_required)
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `pytest tests/test_run_profile.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/run_profile.py \
        tests/test_run_profile.py
git commit -m "feat: run_profile.py executes checks with required-gate"
```

---

### Task 5: Wire the profile into the loop — `SKILL.md`, fallback docs, README/--help

No new code paths to unit-test here; this task documents the agent behavior and surfaces the feature. Verification is by reading the rendered docs and running the existing suite.

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md`
- Modify: `README.md` (repo root)
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py` (`--help` epilog / argparse description for discoverability)

- [ ] **Step 1: Add the "Verification Profile" section to `SKILL.md`**

Insert a new top-level section after the "Optional Judge Eval" section. Use this exact content:

```markdown
## Verification Profile

Each repo can have an opinionated, code-derived **verification profile**: the
checks the loop runs at its verify step. Stored in
`~/.config/gh-gemini-review-loop/preferences.json` under `profiles["owner/repo"]`.

### First run (detect → propose → confirm)

Run detection **after fetching findings, before the first fix attempt**, so the
verification strategy is fixed before any edits. If `get_profile(owner/repo)`
returns `None`:

1. Run `detect_profile.py <repo_root>` to get `{stack, confidence, reasons,
   candidate_checks}`.
2. Reconcile candidates against repo docs (`CLAUDE.md`, `CONTRIBUTING`,
   `README`). If docs pin a non-standard invocation, surface it explicitly:
   *"Repo docs pin `/opt/homebrew/bin/pytest` — use that instead of `pytest`?"*
   Never auto-persist an absolute path from prose without confirmation.
3. Prompt with `AskUserQuestion`:
   *"Detected Python (pyproject.toml, tests/). Proposed: pytest [required],
   ruff check . [required], mypy . [optional]. Confirm / customize / skip?"*
4. Persist via `judge.save_profile(...)`:
   - Confirm → `source="confirmed"` with the candidate checks.
   - Customize → `source="customized"` with the user's edited checks.
   - Skip → `source="skipped"` (no checks).
5. `detect`'s `stack == "unknown"` → do not persist; use ad-hoc verification.

### Subsequent runs

A profile (including a `skipped` one) exists → **no prompt**. If `source` is
`confirmed` or `customized`, run the checks via `run_profile.py`; on `skipped`
or unknown stack, use today's ad-hoc "narrowest meaningful checks".

### Customizing / un-skipping

- *"add mypy to this repo's verification profile"* / *"change the checks to X"* →
  edit the profile via `save_profile(..., source="customized")`.
- *"set up a verification profile for this repo"* → force re-detection even if a
  `skipped` marker exists.

### Gate semantics

The verify step **fails iff any `required` check fails or times out**.
Non-required failures are recorded in `--verification-details` but do not flip
`--verification` to `failed`. Feed `ProfileRunResult.to_details()` into
`--verification-details` and its `verification` field into `--verification`.
```

- [ ] **Step 2: Update the existing verify-step instruction in `SKILL.md`**

Find the numbered verify step (the list item beginning `9. Verify.` with the
sub-bullets "Run the narrowest meaningful checks first."). Replace its body with:

```markdown
9. Verify.
   - If a `confirmed`/`customized` profile exists for this repo, run it with
     `run_profile.py` and apply the required-gate (see "Verification Profile").
   - Otherwise (no profile, `skipped`, or unknown stack): run the narrowest
     meaningful checks first; broaden when shared logic or user-facing behavior
     changes.
   - If checks cannot run, report why and what remains unverified.
```

- [ ] **Step 3: Add NL command rows to the command table in `SKILL.md`**

Find the natural-language command table (the one with the **Persistent cap** and
**Loop + judge** rows) and add:

```markdown
| **Set up verification profile** | "set up a verification profile for this repo" / "configure checks for this repo" | Run `detect_profile.py`, confirm, then `save_profile(..., source="confirmed")` |
| **Customize profile** | "add mypy to this repo's checks" / "change the verification checks to X" | Edit checks, `save_profile(..., source="customized")` |
| **Skip profile** | "skip verification profile" / "use ad-hoc checks for this repo" | `save_profile(repo, source="skipped")` — no re-prompt |
```

- [ ] **Step 4: Add discoverability to `--help` in `fetch_gemini_threads.py`**

In the argparse setup, extend the epilog/description text with one line:

```
Verification profiles: the loop detects a per-repo check profile on first run
(pytest/ruff, npm scripts, cargo, go) and stores it in preferences.json. Say
"set up a verification profile for this repo" to configure, or "skip
verification profile" to opt out.
```

The parser is created as `parser = argparse.ArgumentParser(description=__doc__)`
(~line 1026) with no epilog. Add an `epilog=` argument carrying the paragraph
above, e.g.:

```python
parser = argparse.ArgumentParser(
    description=__doc__,
    epilog=(
        "Verification profiles: the loop detects a per-repo check profile on "
        "first run (pytest/ruff, npm scripts, cargo, go) and stores it in "
        "preferences.json. Say \"set up a verification profile for this repo\" "
        "to configure, or \"skip verification profile\" to opt out."
    ),
    formatter_class=argparse.RawDescriptionHelpFormatter,
)
```

- [ ] **Step 5: Add a README section**

Add a short "Verification profiles" subsection to `README.md` under the existing
feature docs, summarizing: per-repo, detected on first run, confirm/customize/skip,
required-vs-optional checks, stored in `preferences.json`. Mirror the wording from
the SKILL.md section; keep it to one paragraph plus the example JSON profile from
the spec.

- [ ] **Step 6: Verify docs and run the full suite**

Run: `pytest tests/ -q`
Expected: PASS (no regressions).
Manually confirm: `grep -n "Verification Profile" plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md` returns the new section, and `grep -n "verification profile" README.md` returns the new README content.

- [ ] **Step 7: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md \
        plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py \
        README.md
git commit -m "docs(skill): wire verification profile into the loop"
```

---

## Final Verification

- [ ] Run the whole suite once more: `pytest tests/ -q` → all pass.
- [ ] CLI smoke: `python3 .../detect_profile.py .` emits python stack for this repo.
- [ ] Confirm `preferences.json` written by a `save_profile` call shows
      `schema_version: 2` and a `profiles` object (manual: call `save_profile`
      in a throwaway `GGRL_STATE_DIR` and inspect the file).

## Self-Review Notes (author)

- **Spec coverage:** detection (Task 3), persistence + source lifecycle +
  schema v2 (Tasks 1–2), execution/gate with cwd/timeout/required (Task 4),
  control-flow timing + first-run prompt + skip/un-skip + customization +
  discoverability (Task 5). Migration handled in Task 1 (lazy, additive).
- **Absolute-path rule** lives in SKILL.md Task 5 Step 1 (agent reconciliation),
  not in `detect_profile.py`, which only ever emits bare commands — consistent
  with the spec.
- **Type consistency:** `detect()` returns `candidate_checks` with keys
  `{name, command, required}`; `save_profile` stores `checks` with the same
  keys; `run_profile` reads `name`/`command`/`required` and profile-level
  `working_directory`/`timeout_seconds`. Aligned across tasks.
```
