# Verification Profile — Design

**Date:** 2026-06-04
**Status:** Approved (pending spec review)
**Component:** `gh-gemini-review-loop`

## Summary

Add a per-repo **verification profile**: an opinionated, code-derived list of
checks the review loop runs at its verify step. The profile is detected from the
repo's stack on first run, confirmed (or customized, or skipped) by the user,
persisted to `preferences.json` keyed by `owner/repo`, and reused silently on
every later run. It replaces today's ad-hoc *"run the narrowest meaningful
checks"* guess with a deterministic, auditable, per-check-attributable gate.

## Motivation

- **Verification is currently a guess.** `SKILL.md` tells the agent to infer
  checks each run. Results are non-deterministic and not comparable across runs.
- **Non-obvious invocations get missed.** This repo's own `CLAUDE.md` warns that
  `python3 -m pytest` does not work and pins `/opt/homebrew/bin/pytest`. A profile
  captures that once instead of re-deriving it.
- **The metrics layer wants honest data.** `--verification-details` already
  records test output; a fixed profile makes runs comparable and lets summaries
  say *"Failed check: lint"* instead of *"some command failed."*

## Non-Goals (YAGNI)

- **Global reusable named profiles** (a `python`/`frontend` dictionary shared
  across repos). Profiles are repo-derived; reuse is not needed for v1.
- **Repo-checked-in config** (`.gh-gemini-review-loop.json`). Running commands
  from config that travels with a clone is an arbitrary-code-execution surface;
  out of scope until a trust gate exists.
- **Per-check `working_directory` / `timeout_seconds`.** v1 keeps these at the
  profile level. Per-check overrides can come later for monorepos.

## Architecture

Three layers, each with one responsibility:

1. **`detect_profile.py` (new script)** — deterministic stack detection from
   marker files. Pure function of the filesystem; emits a transient JSON
   candidate. Independently unit-testable. Does **not** read or write prefs.
2. **Agent layer (`SKILL.md`)** — runs the detector, reconciles candidates
   against repo docs (`CLAUDE.md` / `CONTRIBUTING` / `README`) to catch
   non-standard invocations, prompts the user, and persists the result.
3. **`judge.py` prefs helpers (extended)** — `judge.py` already owns
   `PREFS_SCHEMA_VERSION`, `prefs_path()`, `load_preferences()`,
   `save_preferences()`. Add `get_profile(repo)` / `save_profile(repo, ...)` and
   bump the schema. `fetch_gemini_threads.py`'s fallback loader
   (`_FALLBACK_PREFS_DEFAULTS`, `load_preferences_with_fallback`) is updated to
   carry `profiles` so a missing `judge` module still surfaces saved profiles.

## Data Model

### Detection output (transient, emitted by `detect_profile.py`)

```json
{
  "stack": "python",
  "confidence": "high",
  "reasons": ["pyproject.toml", "tests/"],
  "candidate_checks": [
    {"name": "tests", "command": "pytest", "required": true},
    {"name": "lint", "command": "ruff check .", "required": true},
    {"name": "typecheck", "command": "mypy .", "required": false}
  ]
}
```

`confidence ∈ {high, medium, low}`. `reasons` lists the markers that drove the
guess, so the agent can explain the proposal. This shape is **never persisted**.

### Persisted profile (`preferences.json`)

```json
{
  "schema_version": 2,
  "profiles": {
    "owner/repo": {
      "detected_stack": "python",
      "source": "confirmed",
      "updated_at": "2026-06-04T12:00:00Z",
      "working_directory": ".",
      "timeout_seconds": 300,
      "checks": [
        {"name": "tests", "command": "pytest", "required": true},
        {"name": "lint", "command": "ruff check .", "required": true},
        {"name": "typecheck", "command": "mypy .", "required": false}
      ]
    }
  }
}
```

- `source ∈ {confirmed, customized, skipped}`. `detected` is transient-only and
  never written. `skipped` profiles carry no `checks`.
- `working_directory` (default `"."`) and `timeout_seconds` (default `300`) are
  profile-level for v1.
- `checks[]` are **objects**, not strings, so each check has a stable `name` for
  per-check attribution and a `required` flag for gate semantics.

## Detection Rules (v1 — four stacks)

| Stack  | Marker(s)                              | Candidate checks                                                |
|--------|----------------------------------------|----------------------------------------------------------------|
| python | `pyproject.toml` / `setup.py` / `tests/` | `pytest` [req]; `ruff check .` [req], `mypy .` [opt] if in deps |
| node   | `package.json`                         | only the existing `test` / `lint` / `typecheck` scripts        |
| rust   | `Cargo.toml`                           | `cargo test` [req], `cargo clippy` [opt]                        |
| go     | `go.mod`                               | `go test ./...` [req], `go vet ./...` [opt]                     |

- **Node:** emit a check only for scripts that actually exist in
  `package.json#scripts`. Never propose `npm run typecheck` if absent.
- **Python optional tools:** include `ruff` / `mypy` only when present in
  declared dependencies (e.g. `pyproject.toml`), not unconditionally.
- **Unknown stack** → no profile proposed; fall back to today's ad-hoc behavior.

### Agent augmentation (the absolute-path rule)

Detected commands are stored **bare** (`pytest`), never as absolute paths.
If repo docs pin a non-standard invocation, the agent surfaces it explicitly:

> Repo docs pin `/opt/homebrew/bin/pytest` — use that instead of `pytest`?

An absolute path is persisted **only** when the user confirms that override, and
the resulting profile is marked `source: "customized"`. The agent never
auto-persists an absolute path from prose.

## Execution Semantics

- Each check's `command` is a single string, executed via `shlex.split` +
  `subprocess` **without a shell**, with `cwd = working_directory` and a
  `timeout_seconds` wall-clock limit. No `shell=True` — avoids injection on
  derived commands. `ruff check .` and friends split cleanly.
- **Gate rule:** the verify step **fails iff any `required` check fails or times
  out.** Non-required check failures are recorded and surfaced but do **not**
  flip `verification` to `failed`.
- A timeout on a required check = failure; on a non-required check = reported,
  non-gating.
- Per-check outcomes (`name`, pass/fail/timeout) feed the existing
  `--verification` (`passed|failed|skipped`) and `--verification-details`
  metrics, enabling summaries like *"Verification: failed — failed check: lint."*

## Control Flow

Detection happens **after fetching findings, before the first fix attempt**, so
the verification strategy is known before any edits (not awkwardly asked at the
verify step after code has already changed). This mirrors the existing one-time
judge-tip insertion point.

```
fetch findings
  → if no profile entry for owner/repo and source != "skipped":
        run detect_profile.py
        agent reconciles candidates with repo docs
        AskUserQuestion:
          "Detected Python (pyproject.toml, tests/). Proposed:
             pytest [required], ruff check . [required], mypy . [optional].
           Confirm / customize / skip?"
        persist profile with source ∈ {confirmed, customized, skipped}
  → fix attempts
  → verify using the profile (cwd, timeout, required-gate)
  → record per-check results into --verification-details
```

- **Subsequent runs:** a profile (or a `skipped` marker) exists → no prompt.
- **Skip escape hatch:** `source: "skipped"` → never re-prompt; use ad-hoc
  verification. Un-skip with *"set up a verification profile for this repo"*,
  which forces re-detection.
- **Customization later:** *"add mypy to this repo's verification profile"* /
  *"change the checks to X"* → agent edits the profile, sets
  `source: "customized"`, updates `updated_at`. Direct JSON edits are honored.

## Schema Migration

Bump `PREFS_SCHEMA_VERSION` `1 → 2`. Migration is additive and lazy:

- A v1 file (no `profiles` key) loads cleanly; absent `profiles` = no profiles.
- On first `save_profile`, write `schema_version: 2` and the `profiles` object.
- `_FALLBACK_PREFS_DEFAULTS` in `fetch_gemini_threads.py` gains
  `"profiles": {}` so the fallback loader never KeyErrors on the new key.

## Testing

- **`detect_profile.py`** — unit tests per stack using temp-dir fixtures with the
  relevant marker files; assert `stack`, `confidence`, `reasons`,
  `candidate_checks`. Include: node script-presence filtering, python optional-tool
  gating, and unknown-stack → empty/`low` result.
- **Prefs helpers** — `get_profile` / `save_profile` round-trip; `source`
  lifecycle (`confirmed`/`customized`/`skipped`); v1→v2 migration (load a v1 file,
  save a profile, assert `schema_version: 2`); fallback loader carries `profiles`.
- **Execution/gate** — required-fail → gate fails; non-required-fail → gate
  passes but is recorded; timeout enforcement; `working_directory` honored.
- Tests run under the repo's pytest (`/opt/homebrew/bin/pytest` or venv), per
  `CLAUDE.md`.

## Files Touched

- **New:** `scripts/detect_profile.py` + `tests/test_detect_profile.py`.
- **`scripts/judge.py`** — `PREFS_SCHEMA_VERSION → 2`; add `get_profile`,
  `save_profile`; profile-aware load/save.
- **`scripts/fetch_gemini_threads.py`** — `_FALLBACK_PREFS_DEFAULTS` gains
  `profiles`; fallback loader carries it.
- **`SKILL.md`** — new "Verification Profile" section; verify-step changes;
  detection-before-first-fix timing; NL command-table rows (set up / customize /
  skip / un-skip).
- **`README` / `--help`** — discoverability.
- **Tests** — `tests/test_judge_doctor.py` / prefs tests updated for schema v2.
