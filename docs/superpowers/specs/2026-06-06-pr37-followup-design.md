# PR-37 Follow-up — Four Testing Findings

**Date:** 2026-06-06
**Branch:** `feat/enforce-loop-visibility` (PR #37)
**Status:** Approved design

## Context

PR #37 (`feat(hooks): mechanically guarantee loop visibility + profile-before-fixes`)
moved two most-skipped agent obligations from SKILL.md prose into bundled hooks.
Live testing of the PR surfaced four findings. This spec addresses all four.

Three are prose/agent-behavior fixes (findings 1, 2, 4 — though 4 has a small
mechanical script change). One is a real code change to the verification-profile
detector (finding 3).

The guiding principle from PR-37 holds: **if a constraint is mechanical, automate
it; do not leave a memory-dependent obligation as prose.**

---

## Finding 1 — Initial-trigger assumption

### Problem

When resuming a loop, the agent waited for Gemini's *initial* review even though
that review had already happened, or failed to trigger cycle 1 when it had not.

### Fix (prose — `SKILL.md` "Recovery: Missed Initial Trigger")

Reword so the **default assumption is that cycle 0 (Gemini's initial automatic
review) already happened.** The agent only triggers cycle 1 itself when there is
**no Gemini review activity on the branch at all**.

- Resume entry point: check for any existing Gemini review activity on the PR.
- If Gemini review activity exists → do **not** re-wait for an initial review;
  proceed to fetch threads and run the normal cycle.
- If **no** Gemini review activity exists anywhere on the PR → trigger the first
  review (cycle 1) ourselves, then wait.

This removes the "wait forever for an initial review that is already done"
failure mode.

---

## Finding 2 — Resume after the cap

### Problem

Re-invoking the loop on a PR already at the cap was treated as an instant STOP.
In practice, re-invocation is usually a **resume signal**: the user bumped the
cap, or a prior cycle was interrupted mid-flight.

### Fix (prose now — `SKILL.md` new "Resuming after the cap" subsection)

At cap, branch on exactly these four cases, in order:

| # | Condition | Action |
|---|-----------|--------|
| 1 | User increased the cap (effective `max_rereview_requests` > the cap already consumed) | **Continue** from the next cycle |
| 2 | Interrupted local work **not pushed** (local commits/edits exist beyond the remote branch HEAD) | **Finish the push** — no new cycle consumed |
| 3 | **Pushed** but no re-review request posted for that pushed SHA | **Request review** for that SHA — no new cycle consumed |
| 4 | No new local work **and** no higher cap | **Hard stop** (today's behavior) |

Cases 2 and 3 do **not** consume a new cycle — they complete an
already-started cycle whose final step was interrupted.

### Deterministic-detection obligation (follow-up, flagged in spec)

Cases 2 and 3 are **prose-detected now** but MUST become deterministic so
"resume" does not drift between sessions:

- **Case 2** needs a **local-vs-remote SHA comparison** (e.g. `git rev-parse HEAD`
  vs `git rev-parse @{u}` / the PR's remote branch tip) to know whether local
  work is unpushed.
- **Case 3** needs a **GitHub timestamp/SHA check**: was the most recent push's
  SHA followed by any agent-posted `@gemini-code-assist` re-review comment? If no
  re-review comment exists at or after the pushed commit's timestamp, case 3
  applies.

This is recorded as a follow-up obligation, not open-ended prose. Prose is
acceptable as the *interim* implementation only.

---

## Finding 3 — Monorepo test-path detection (real code change)

### Problem

`detect_profile.py` only inspects root-level markers and emits a single
root-cwd check. In a monorepo it found only `test-backend` and missed
`familia-ai/client/tests` and `familia-ai/scraper-svc/src/__tests__`. It also
ignored the repo's `justfile`, which maps the real test paths.

### Schema change — per-check `working_directory`

Add an **optional** `working_directory` to each check object:

```json
{
  "checks": [
    {"name": "backend", "command": "pytest", "working_directory": "test-backend", "required": true},
    {"name": "client",  "command": "npm test", "working_directory": "familia-ai/client", "required": true}
  ]
}
```

`run_profile._run_one` resolves cwd as:

```
root / (check.working_directory or profile.working_directory or ".")
```

Fully backward compatible: existing single-dir profiles set no per-check
`working_directory` and behave exactly as today (all checks share the
profile-level `working_directory`).

### Detection — strict precedence (NOT a union)

Two discovery sources with **strict either/or precedence** — no union, no
dedup logic in this iteration:

1. **Justfile mode (authoritative when present).** Parse recipe names defined at
   column 0 of a `justfile` / `Justfile` / `.justfile`. A recipe is a
   verification recipe if its name matches (case-insensitive):
   `test`, `test-*`, `*-test`, `*-tests`, `check`, `lint`, `typecheck`, `verify`.
   For each match, emit:
   ```json
   {"name": "<recipe>", "command": "just <recipe>", "working_directory": ".", "required": true}
   ```
   No recipe-body parsing — `just` itself handles `cd`, flags, and env.
   **If ≥1 matching recipe exists, emit those recipes and SKIP git-tree
   discovery entirely.**

2. **Git-tree mode (only when no matching justfile recipes exist).**
   `git ls-files` to enumerate tracked files (this naturally skips
   `node_modules`, `.venv`, etc.). Find tracked directories named `tests`,
   `test`, `__tests__`, `spec`, or `specs`. For each test dir, walk **up** to the
   nearest package marker and map to a runner + cwd:
   - `package.json` → `npm test`, cwd = the marker's directory
   - `pyproject.toml` / `setup.py` → `pytest`, cwd = the marker's directory
   - `Cargo.toml` → `cargo test`, cwd = the marker's directory
   - `go.mod` → `go test ./...`, cwd = the marker's directory

   If no package marker is found walking up from a test dir, skip that dir
   (cannot determine a runner deterministically).

3. **Root single-stack fallback (unchanged).** When neither a justfile recipe
   nor a git-tree test dir yields checks, fall back to today's root-marker
   single-stack detection (`_detect_python` / `_detect_node` / etc.).

### Future (noted, NOT built now)

Recipe-body parsing and/or path heuristics could let justfile recipes be
deduped against discovered package paths so both sources could be unioned
safely. Out of scope for this iteration — strict precedence avoids the dedup
problem entirely for now.

### Presets

`build_presets` keeps its current shape. "All detected" now lists all emitted
checks (possibly several). Long joined command labels are truncated for menu
display so the `AskUserQuestion` option label stays readable. "Tests only" /
"First check only" narrowing logic is unchanged.

---

## Finding 4 — `on_cycle` eval ran only at completion

### Problem

The user selected `judge_mode: on_cycle`, but the judge ran only at loop end.
Root cause: `--judge-phase` defaults to `None`; the agent passed
`--judge-phase complete` only at the terminal `--record-run` and never passed
`--judge-phase cycle` on the per-cycle fetches. With `should_judge_run("on_cycle",
None)` → `False`, the per-cycle judge silently never ran. The script logic is
correct; the failure is agent memory.

### Fix (mechanical — `fetch_gemini_threads.py`)

Infer the phase from invocation context when `--judge-phase` is `None`:

- `--record-run` present → phase = `"complete"`
- otherwise (a normal cycle fetch) → phase = `"cycle"`

Result: `on_cycle` runs on every normal cycle fetch and `on_complete` runs only
at the terminal record-run, with **zero agent memory required**. An explicit
`--judge-phase` flag still wins (override preserved).

SKILL.md updated to state the phase is auto-inferred from invocation context, so
the agent no longer needs to remember to pass it.

---

## Testing (TDD)

Per `CLAUDE.md`, run via `/opt/homebrew/bin/pytest`. Each test watched to fail
first.

**`run_profile` per-check cwd**
- check with `working_directory` runs in that dir
- check without `working_directory` falls back to profile-level `working_directory`
- mixed list: some checks per-dir, some inherit

**`detect_profile` justfile mode**
- recipe name matching (`test`, `test-*`, `*-tests`, `check`, `lint`, `typecheck`, `verify`); non-matching recipes (`build`, `deploy`) excluded
- emits `just <recipe>` checks with `working_directory: "."`
- presence of ≥1 matching recipe suppresses git-tree discovery
- `justfile` / `Justfile` / `.justfile` filename variants

**`detect_profile` git-tree mode**
- discovers `tests` / `test` / `__tests__` / `spec(s)` dirs from tracked files
- nearest-marker mapping: `package.json` → npm, `pyproject.toml`/`setup.py` → pytest, `Cargo.toml` → cargo, `go.mod` → go, each with correct cwd
- test dir with no package marker up-tree is skipped
- git-tree mode runs only when no justfile recipes matched

**`fetch_gemini_threads` judge-phase inference**
- `--judge-phase None` + `--record-run` → effective phase `complete`
- `--judge-phase None` + normal fetch → effective phase `cycle`
- explicit `--judge-phase cycle`/`complete` still wins over inference

Findings 1 and 2 are prose-only in `SKILL.md` (no new unit tests beyond a
read-through that the wording is internally consistent). Full suite must stay
green; ruff clean; scripts compile.
