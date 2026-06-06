# Verification Profile — Preset-Menu Setup (v1) — Design

**Date:** 2026-06-05
**Status:** Approved (pending spec review)
**Component:** `gh-gemini-review-loop`
**Supersedes:** the first-run prompt in `2026-06-04-verification-profile-design.md`

## Summary

Replace the profile's first-run **confirm / customize / skip** prompt with a
**single-select preset menu** built deterministically from detected checks. After
`detect_profile.py` runs, the agent presents explicit preset bundles — "All
detected", optionally "Tests only", "Skip", and "Customize manually" — each gating
check treated as a **required gate**. Every option is constructed by code
(`build_presets`); nothing relies on the tool auto-adding an option. The chosen
preset is persisted exactly as today (`judge.save_profile`); the scripts'
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
3. Call `build_presets(candidate_checks)` (rule below) to get the explicit option
   list, then prompt once via `AskUserQuestion`.
4. Persist the chosen preset via `judge.save_profile(...)`:
   - **All detected** → `source="confirmed"`, all checks `required: true`.
   - A narrower preset (e.g. "Tests only") → `source="customized"`, selected
     checks `required: true`.
   - **Skip** → `source="skipped"`, `checks=[]` (remembered; see below).
   - **Customize manually** → route to the free-form NL customize path →
     `source="customized"`.

### Preset generation rule — `build_presets(candidate_checks)`

Preset construction is **deterministic code**, not agent prose. `build_presets`
takes the detector's `candidate_checks` (ordered; `tests` first by construction)
and returns the explicit ordered option list. Do **not** depend on the
`AskUserQuestion` tool auto-adding an "Other"/escape option — "Customize manually"
is an option we emit ourselves.

Components:

- **"All detected"** — every candidate check, each `required: true`. Always
  present; labeled recommended. Option label lists the commands, e.g.
  *"All detected — pytest + ruff check ."*
- **"Tests only"** (multi-check, has a `tests` check) / **"First check only"**
  (multi-check, no check named `tests` → the first candidate). **Omitted** when it
  would be identical to "All detected" (i.e. a single detected check), so no
  duplicate option is ever shown.
- **"Skip — use ad-hoc verification"** — always present.
- **"Customize manually"** — always present; routes to the free-form NL customize
  path.

Resulting menus (always within the `AskUserQuestion` 2–4-option limit):

- **Single check** → All detected · Skip · Customize manually *(3)*
- **Multi-check with `tests`** → All detected · Tests only · Skip · Customize
  manually *(4)*
- **Multi-check without `tests`** → All detected · First check only · Skip ·
  Customize manually *(4)*

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
exists, so the loop uses **ad-hoc verification** (the narrowest-meaningful-checks
fallback) — not "run nothing."

`source="skipped"` suppresses **automatic** detection prompts only. **Explicit
user intent overrides it** and re-runs detection: an NL command such as *"set up a
verification profile for this repo"* re-enters the detect → preset-menu → save
flow and overwrites the skipped marker. So skip is sticky against auto-prompting
but never blocks a deliberate setup request.

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
- **`detect_profile.py`** — unchanged detection logic. **Add** the pure helper
  `build_presets(candidate_checks) -> list[preset]` so preset construction is
  deterministic and unit-testable in code, not agent-side prose. It does not
  read/write prefs. Each returned preset carries a stable label, the gating
  `checks`, the `source` to persist on selection (`confirmed`/`customized`/
  `skipped`), and a flag marking the "Customize manually" escape (no checks; hands
  off to the NL path).
- **`judge.py`** — unchanged. `save_profile` / `get_profile` already cover all
  three sources.
- **`run_profile.py`** — unchanged. With all saved checks `required: true`, any
  failure already gates, which is exactly "checked = gates."

## Testing

- **`build_presets` (unit)**, asserting exact ordered option labels:
  - single check → `All detected` · `Skip` · `Customize manually` (no duplicate).
  - multi-check with `tests` → `All detected` · `Tests only` · `Skip` ·
    `Customize manually`.
  - multi-check without `tests` → `All detected` · `First check only` · `Skip` ·
    `Customize manually`.
  - unknown stack / empty `candidate_checks` → empty list (no menu).
- **Source lifecycle**: All detected → `confirmed`; narrower preset → `customized`;
  Skip → `skipped` with `checks: []`; Customize manually → escape flag set, no
  persisted checks.
- **Remembered skip**: a `skipped` profile suppresses *automatic* re-detection and
  routes to ad-hoc verification, **but** an explicit "set up a verification
  profile" request overrides it and re-runs detection.
- **Absolute path not auto-saved**: a doc-pinned `/opt/homebrew/bin/pytest` is not
  persisted unless explicitly selected.
- Existing `detect_profile` / `judge` / `run_profile` / integration suites stay
  green.
