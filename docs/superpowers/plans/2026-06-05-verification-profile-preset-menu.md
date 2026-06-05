# Verification Profile Preset-Menu (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the verification profile's first-run "confirm / customize / skip" prompt with a deterministic, code-built preset menu (All detected / narrower / Skip / Customize manually).

**Architecture:** Add one pure function `build_presets(candidate_checks)` to `detect_profile.py` that turns the detector's candidate checks into an explicit ordered option list, and expose it through the script's CLI JSON output. The agent (`SKILL.md`) renders those options via `AskUserQuestion` and persists the chosen one through the existing `judge.save_profile`. `judge.py` and `run_profile.py` are unchanged; gate semantics already match (every saved check is `required: true`).

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), pytest. Scripts live in `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/`. Tests in `tests/` import scripts by bare module name (a conftest adds the scripts dir to `sys.path`).

**Test runner:** `/opt/homebrew/bin/pytest` (or `pytest` from a venv). `python3 -m pytest` does NOT work in this repo.

---

## File Structure

- **Modify** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_profile.py`
  — add pure helpers `_required`, `_commands_label`, `build_presets`; extend `main()` to add a `presets` key to the emitted JSON. Detection logic untouched.
- **Modify** `tests/test_detect_profile.py` — add `build_presets` unit tests and one CLI-output test.
- **Modify** `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md` — rewrite the "First run" section to detect → preset menu → save; tighten skip/override wording; update the NL-commands list.
- **Modify** `README.md` — update the "Verification profiles" paragraph to describe the preset menu and remembered skip.

No new files. `judge.py`, `run_profile.py`, and the integration tests are unchanged.

---

## Task 1: `build_presets` helper + CLI exposure

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_profile.py`
- Test: `tests/test_detect_profile.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_detect_profile.py`:

```python
from detect_profile import build_presets, detect, main


def _labels(presets):
    return [p["label"] for p in presets]


def test_build_presets_empty_candidates_returns_no_menu():
    assert build_presets([]) == []


def test_build_presets_single_check_has_no_narrower_option():
    candidates = [{"name": "tests", "command": "cargo test", "required": True}]
    presets = build_presets(candidates)
    assert _labels(presets) == [
        "All detected — cargo test",
        "Skip — use ad-hoc verification",
        "Customize manually",
    ]


def test_build_presets_multi_check_with_tests_offers_tests_only():
    candidates = [
        {"name": "tests", "command": "pytest", "required": True},
        {"name": "lint", "command": "ruff check .", "required": True},
    ]
    presets = build_presets(candidates)
    assert _labels(presets) == [
        "All detected — pytest + ruff check .",
        "Tests only — pytest",
        "Skip — use ad-hoc verification",
        "Customize manually",
    ]


def test_build_presets_multi_check_without_tests_offers_first_check_only():
    candidates = [
        {"name": "lint", "command": "npm run lint", "required": True},
        {"name": "typecheck", "command": "npm run typecheck", "required": False},
    ]
    presets = build_presets(candidates)
    assert _labels(presets) == [
        "All detected — npm run lint + npm run typecheck",
        "First check only — npm run lint",
        "Skip — use ad-hoc verification",
        "Customize manually",
    ]


def test_build_presets_forces_required_true_on_all_gating_checks():
    # mypy is optional in detection (required=False); a gate must persist as required.
    candidates = [
        {"name": "tests", "command": "pytest", "required": True},
        {"name": "typecheck", "command": "mypy .", "required": False},
    ]
    presets = build_presets(candidates)
    all_detected = presets[0]
    assert all_detected["source"] == "confirmed"
    assert all(c["required"] is True for c in all_detected["checks"])


def test_build_presets_sources_and_customize_flag():
    candidates = [
        {"name": "tests", "command": "pytest", "required": True},
        {"name": "lint", "command": "ruff check .", "required": True},
    ]
    by_label = {p["label"]: p for p in build_presets(candidates)}
    assert by_label["All detected — pytest + ruff check ."]["source"] == "confirmed"
    assert by_label["Tests only — pytest"]["source"] == "customized"
    skip = by_label["Skip — use ad-hoc verification"]
    assert skip["source"] == "skipped" and skip["checks"] == []
    customize = by_label["Customize manually"]
    assert customize["customize"] is True and customize["source"] is None


def test_main_output_includes_presets_key(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['ruff']\n"
    )
    (tmp_path / "tests").mkdir()
    rc = main(["detect_profile.py", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # additive: existing keys still present, plus a ready-made menu.
    assert payload["stack"] == "python"
    assert payload["candidate_checks"]  # unchanged contract
    assert [p["label"] for p in payload["presets"]] == [
        "All detected — pytest + ruff check .",
        "Tests only — pytest",
        "Skip — use ad-hoc verification",
        "Customize manually",
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_detect_profile.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_presets'` (and `main` is importable already, but the new assertions can't run until the function exists).

- [ ] **Step 3: Implement `build_presets` and the helpers**

In `detect_profile.py`, add these functions just above `def detect(` (after the `_detect_go` block):

```python
def _required(check: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``check`` forced to required=True.

    v1 has a single gating tier: every check in a saved profile is required.
    Detection may mark a check optional (e.g. mypy); when it becomes a gate we
    normalize it to required so persistence and run_profile gate on it.
    """
    return {"name": check["name"], "command": check["command"], "required": True}


def _commands_label(checks: list[dict[str, Any]]) -> str:
    return " + ".join(c["command"] for c in checks)


def build_presets(candidate_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the explicit ordered preset menu from detector candidate checks.

    Pure function. Returns a list of preset dicts, each shaped::

        {"label": str, "checks": list[check], "source": str | None,
         "customize": bool}

    Rules:
    - Empty candidates (unknown stack) -> ``[]``; the caller shows no menu and
      falls back to ad-hoc verification.
    - "All detected" -> every candidate check, each required=True,
      ``source="confirmed"``. Always present.
    - A narrower option appears only for multi-check repos (omitted when it
      would duplicate "All detected"): "Tests only" when a check named ``tests``
      exists, else "First check only" (the first candidate). ``source="customized"``.
    - "Skip - use ad-hoc verification" -> ``source="skipped"``, no checks.
    - "Customize manually" -> escape option (``customize=True``, ``source=None``,
      no checks); the caller hands off to the free-form NL customize path.

    Every option is emitted here. Nothing relies on the prompt tool auto-adding
    an "Other"/escape option.
    """
    if not candidate_checks:
        return []
    all_checks = [_required(c) for c in candidate_checks]
    presets: list[dict[str, Any]] = [
        {
            "label": f"All detected — {_commands_label(all_checks)}",
            "checks": all_checks,
            "source": "confirmed",
            "customize": False,
        }
    ]
    if len(candidate_checks) > 1:
        tests = next((c for c in candidate_checks if c["name"] == "tests"), None)
        narrow = _required(tests if tests is not None else candidate_checks[0])
        label_prefix = "Tests only" if tests is not None else "First check only"
        presets.append(
            {
                "label": f"{label_prefix} — {narrow['command']}",
                "checks": [narrow],
                "source": "customized",
                "customize": False,
            }
        )
    presets.append(
        {
            "label": "Skip — use ad-hoc verification",
            "checks": [],
            "source": "skipped",
            "customize": False,
        }
    )
    presets.append(
        {
            "label": "Customize manually",
            "checks": [],
            "source": None,
            "customize": True,
        }
    )
    return presets
```

- [ ] **Step 4: Expose presets through the CLI**

In `detect_profile.py`, change `main` so the emitted JSON carries the ready-made menu (additive — existing keys unchanged):

```python
def main(argv: list[str]) -> int:
    root = argv[1] if len(argv) > 1 else "."
    result = detect(root)
    result["presets"] = build_presets(result["candidate_checks"])
    print(json.dumps(result, indent=2))
    return 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_detect_profile.py -q`
Expected: PASS — all existing `detect` tests plus the new `build_presets` and `main` tests green.

- [ ] **Step 6: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_profile.py tests/test_detect_profile.py
git commit -m "feat(profile): build_presets menu + CLI presets output

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Rewrite the SKILL.md first-run flow

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md`

This task is documentation/agent-instruction prose; there is no automated test. Verify by reading the rendered section and confirming it matches the spec.

- [ ] **Step 1: Replace the "First run" and "Customizing / un-skipping" subsections**

Find this block (under `## Verification Profile`):

```markdown
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
```

Replace it with:

```markdown
### First run (detect → preset menu → save)

Run detection **after fetching findings, before the first fix attempt**, so the
verification strategy is fixed before any edits. If `get_profile(owner/repo)`
returns `None`:

1. Run `detect_profile.py <repo_root>`. It returns `{stack, confidence, reasons,
   candidate_checks, presets}`. `presets` is an explicit, ordered, code-built
   option list — do **not** hand-roll the menu or rely on `AskUserQuestion`
   auto-adding an option.
2. If `stack == "unknown"` (empty `candidate_checks`, empty `presets`) → do
   **not** prompt or persist; use ad-hoc verification.
3. Reconcile against repo docs (`CLAUDE.md`, `CONTRIBUTING`, `README`). If docs
   pin a non-standard invocation, surface it as a note beside the menu — *"Repo
   docs pin `/opt/homebrew/bin/pytest`; pick **Customize manually** to use it."*
   Never auto-persist an absolute path from prose.
4. Prompt once with `AskUserQuestion`, using each `presets[i].label` verbatim as
   an option. Example menu for a multi-check Python repo:
   *"All detected — pytest + ruff check ." / "Tests only — pytest" / "Skip — use
   ad-hoc verification" / "Customize manually"*.
5. Persist the chosen preset via `judge.save_profile(...)`:
   - Has `customize == true` (**Customize manually**) → run the free-form NL
     customize path; persist the user's edited checks with `source="customized"`.
   - Otherwise persist `preset["checks"]` with `source=preset["source"]`
     (`confirmed` for All detected, `customized` for a narrower preset, `skipped`
     for Skip). Every persisted check is `required: true`.
```

- [ ] **Step 2: Replace the "Customizing / un-skipping" subsection**

Find:

```markdown
### Customizing / un-skipping

- *"add mypy to this repo's verification profile"* / *"change the checks to X"* →
  edit the profile via `save_profile(..., source="customized")`.
- *"set up a verification profile for this repo"* → force re-detection even if a
  `skipped` marker exists.
```

Replace with:

```markdown
### Customizing / un-skipping

`source="skipped"` suppresses **automatic** detection prompts only. Explicit user
intent always overrides it:

- *"add mypy to this repo's verification profile"* / *"change the checks to X"* →
  edit the profile via `save_profile(..., source="customized")`.
- *"set up a verification profile for this repo"* → re-enter the detect → preset
  menu → save flow and overwrite the profile, **even if** a `skipped` marker
  exists.
```

- [ ] **Step 3: Verify the rendered section**

Run: `grep -n "preset menu\|Customize manually\|suppresses \*\*automatic" plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md`
Expected: matches in the rewritten "First run" and "Customizing / un-skipping" subsections.

- [ ] **Step 4: Confirm the full suite still passes (no script behavior changed)**

Run: `/opt/homebrew/bin/pytest tests/ -q`
Expected: PASS (same count as before plus Task 1's new tests).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md
git commit -m "docs(skill): preset-menu first-run flow; precise skip override

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Update the README

**Files:**
- Modify: `README.md`

Documentation only; no automated test.

- [ ] **Step 1: Replace the "Verification profiles" paragraph**

Find the paragraph beginning *"The loop can detect a per-repo **verification profile** on first run"* and ending *"… or "skip verification profile" to opt out and stay on ad-hoc checks."* Replace it with:

```markdown
The loop can detect a per-repo **verification profile** on first run — the exact
checks to run at the verify step (pytest/ruff, npm scripts, cargo, go test). On
the first cycle with actionable findings, the agent scans the repo for signals
(pyproject.toml, package.json, Cargo.toml, go.mod) and presents a short **preset
menu**: *All detected*, a narrower *Tests only* / *First check only* (multi-check
repos), *Skip — use ad-hoc verification*, and *Customize manually*. Picking a
preset persists it under `profiles["owner/repo"]` in
`~/.config/gh-gemini-review-loop/preferences.json`; later runs skip the prompt and
run the saved checks. In v1 every saved check is a required gate — failure flips
the verify step to `--verification failed`. **Skip is remembered**: it suppresses
the automatic prompt and stays on ad-hoc checks, but saying *"set up a
verification profile for this repo"* re-runs detection and overrides it.
```

- [ ] **Step 2: Fix the JSON example to reflect required-only v1**

The example currently ends with a non-required mypy line, which v1 no longer
persists. Change:

```json
        {"name": "typecheck", "command": "mypy .", "required": false}
```

to:

```json
        {"name": "typecheck", "command": "mypy .", "required": true}
```

Then confirm no stray non-required gate remains in the example:

Run: `grep -n '"required": false' README.md`
Expected: NO matches.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): preset-menu verification profile + remembered skip

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the full suite: `/opt/homebrew/bin/pytest tests/ -q` → all green (previous count + 7 new tests from Task 1).
- [ ] `git log --oneline` shows three new commits (helper+CLI, SKILL.md, README) on `feat/verification-profile`.
- [ ] Spot-check: `python3 plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_profile.py .` from the repo root prints a `presets` array whose labels match the multi-check-with-tests shape (this repo is Python with `pyproject.toml` + `tests/`).
