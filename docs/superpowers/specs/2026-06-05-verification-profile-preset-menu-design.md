# Verification Profile — Preset-Menu Setup (v1) — Design

**Date:** 2026-06-05
**Status:** Approved (pending spec review)
**Component:** `gh-gemini-review-loop`
**Supersedes:** the first-run prompt in `2026-06-04-verification-profile-design.md`

## Summary

Replace the profile's first-run **confirm / customize / skip** prompt with a
**single-select preset menu** built deterministically from detected checks. After
`detect_profile.py` runs, the agent presents 2–4 preset bundles — "All detected",
optionally "Tests only", and "Skip" — each treated as a **required gate**. The
chosen preset is persisted exactly as today (`judge.save_profile`); the scripts'
storage and execution layers are unchanged. This makes the setup step legible to
the user (they see the actual checks and pick one bundle) while staying inside the
`AskUserQuestion` tool's 2–4-option, single-select limit.

## Motivation

- **The merged prompt is all-or-nothing.** "Confirm / customize / skip" hides the
  individual checks behind a yes/no and routes any change through free-form text.
  Users want to *see the detected checks and pick which apply* in one step.
- **`AskUserQuestion` can't do true checkboxes.** It wants 2–4 options and a
  single explicit choice. Presets are the honest fit: bundle the detected checks
  into a few named choices instead of faking a multi-select.
- **Simpler gate semantics.** Collapsing to "a check is a gate or it's off" removes
  the optional/non-gating tier that v1 does not need.

## Non-Goals (YAGNI)

- **Optional / non-gating checks.** Dropped for v1. Every persisted check is a
  required gate. (The schema keeps the `required` field for forward-compat, always
  `true` in v1.)
- **Per-check checkbox UI.** Out — superseded by presets due to tool limits.
- **In-prompt command editing or adding undetected checks.** Stays on the existing
  free-form NL customize path ("change this repo's checks to …").
- **Auto-persisting doc-pinned absolute paths.** Never. Surfaced as a note only;
  persisted only when the user explicitly selects/customizes it.

## Behavior

### First run (detect → preset menu → save)

Detection runs **after fetching findings, before the first fix attempt**, exactly
as in the merged design. If `get_profile(owner/repo)` returns `None`:

1. Run `detect_profile.py <repo_root>` → `{stack, confidence, reasons,
   candidate_checks}`.
2. If `stack == "unknown"` (empty `candidate_checks`) → do **not** prompt or
   persist; fall back to ad-hoc verification.
3. Build presets from `candidate_checks` (rule below) and prompt once via
   `AskUserQuestion`.
4. Persist the chosen preset via `judge.save_profile(...)`:
   - **All detected** → `source="confirmed"`, all checks `required: true`.
   - A narrower preset → `source="customized"`, selected checks `required: true`.
   - **Skip** → `source="skipped"`, `checks=[]` (remembered; see below).
   - **Other** (auto-added) → route to the free-form NL customize path →
     `source="customized"`.

### Preset generation rule

Given `candidate_checks` (ordered; `tests` first by construction):

- **"All detected"** — every candidate check, each `required: true`. Always
  present; labeled recommended. Option label lists the commands, e.g.
  *"All detected — pytest + ruff check ."*
- **"Tests only"** — only the `tests` check (or the first check if none is named
  `tests`). **Omitted** when it would be identical to "All detected" (i.e. a
  single detected check), so no duplicate option is ever shown.
- **"Skip — use ad-hoc verification"** — always present.
- **"Other"** — auto-provided by the tool; routes to NL customize.

This yields 2 options (single-check repos: All detected + Skip) to 3 options
(multi-check repos: All detected + Tests only + Skip), always within the 2–4 limit.

### Doc reconciliation (absolute paths)

Detection only ever proposes the standard command (`pytest`, `npm test`, …). When
repo docs (`CLAUDE.md` / `CONTRIBUTING` / `README`) pin a non-standard invocation
(this repo: `/opt/homebrew/bin/pytest`), the agent surfaces it as a one-line note
beside the menu and points to the NL customize command. The absolute path is
**persisted only if the user explicitly chooses/customizes it** — never written
from prose automatically.

### Gate semantics

- A check in the saved profile **runs at the verify step and gates the loop**:
  any failure (or timeout) ⇒ `--verification failed`.
- A check not in the chosen preset does not run.
- No optional/report-only tier in v1.

### Skip is remembered

Skip persists `source="skipped"` with `checks=[]`. On later runs the profile
exists, so the loop **does not re-prompt** and uses **ad-hoc verification** (the
narrowest-meaningful-checks fallback) — not "run nothing." Re-detection is
available via the NL command *"set up a verification profile for this repo."*

## Data Model

No schema change from the merged design. A persisted profile:

```json
{
  "profiles": {
    "owner/repo": {
      "detected_stack": "python",
      "source": "confirmed",
      "working_directory": ".",
      "timeout_seconds": 300,
      "checks": [
        {"name": "tests", "command": "pytest", "required": true},
        {"name": "lint", "command": "ruff check .", "required": true}
      ]
    }
  }
}
```

In v1 every entry in `checks` has `required: true`. A `skipped` profile has
`checks: []`.

## Architecture / Surface Area

- **`SKILL.md`** — rewrite the "First run (detect → propose → confirm)" section to
  "detect → preset menu → save"; document the preset-generation rule, the
  remembered-skip semantics, and the unchanged NL customize/skip commands. Update
  the NL-commands table.
- **`README.md`** — update the "Verification profiles" paragraph to describe the
  preset menu and remembered skip.
- **`detect_profile.py`** — unchanged detection logic. Optionally add a small pure
  helper (e.g. `build_presets(candidate_checks) -> list[preset]`) so preset
  construction is deterministic and unit-testable rather than agent-side prose;
  if added, it does not read/write prefs.
- **`judge.py`** — unchanged. `save_profile` / `get_profile` already cover all
  three sources.
- **`run_profile.py`** — unchanged. With all saved checks `required: true`, any
  failure already gates, which is exactly "checked = gates."

## Testing

- **Preset generation** (if `build_presets` added): multi-check stack → All
  detected + Tests only; single-check stack → All detected only (no duplicate);
  unknown stack → empty (no menu).
- **Source lifecycle**: All detected → `confirmed`; narrower preset → `customized`;
  Skip → `skipped` with `checks: []`.
- **Remembered skip**: a `skipped` profile suppresses re-detection and routes to
  ad-hoc verification.
- **Absolute path not auto-saved**: a doc-pinned `/opt/homebrew/bin/pytest` is not
  persisted unless explicitly selected.
- Existing `detect_profile` / `judge` / `run_profile` / integration suites stay
  green.
