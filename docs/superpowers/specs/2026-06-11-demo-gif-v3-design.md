# Demo GIF v3 — 36s chaptered story

## Goal

Replace `docs/gh-gemini-review-loop-demo.gif` with a ~36s demo that mirrors the real
AegisLocal PR #12 loop run. Five beats, three money frames (repo-aware gate, judge,
stats). All output wording lifted from real loop output: cycle receipts, judge
verdicts, purple `[loop]` lines, stats table.

## Format

- Hand-authored asciinema v2 `.cast` (same pipeline as v2 demo): 96×36,
  `docs/gh-gemini-review-loop-demo.cast` is the source of truth.
- Render: `agg` → `docs/gh-gemini-review-loop-demo.gif`. Keep GIF < 1 MB.
- Visual language: Claude-Code-style transcript (`>` prompt, `⏺ Skill(...)`,
  `⎿` progress lines, boxed money frames, dim chapter feel). Holds of 2–3s on
  each money frame via event timestamps.

## Flow (locked)

### 0–3s — Hook
- `PR #5 · feat/model-provenance-scanning`
- `gemini-code-assist: 6 findings`
- `> run CR loop with eval` → `⏺ Skill(gh-gemini-review-loop --judge-mode once)`

### 3–9s — Money frame 1: repo-aware gate (hold ~3s)
Boxed frame:
- `probing: Python ✓ (uv)   Node ✗   Rust ✗   Go ✗`
- `gate: uv run pytest  (required — runs every cycle)`
- `no push unless tests pass`

### 9–18s — Money frame 2: judge filters noise (hold ~3s)
- `cycle 1/3  fetched 6 findings`
- `judge classified 6: 5 valid_actionable · 1 false_positive`
- Boxed judge card: gemini claim vs judge reasoning → `1 false positive skipped`
  (reply posted, thread `ADDRESSED_BY_REPLY`)
- `5 fixes applied · verification: uv run pytest → passed · pushed · re-review requested`

### 18–27s — Money frame 3: cycle 2 + human pull-in (hold ~3s)
- `gemini re-reviewed: 2 new findings (high: 2)` → judge agrees → both fixed,
  verification passed
- Receipt fragment with `Semantic risk note` (real wording: behavior-changing fix)
- Purple `[loop]` line: `verification passed, but this may require human review`
  + the one thread link → human pulled in only where needed

### 27–33s — Clean re-review
- `re-review requested` → `gemini re-reviewed: no additional feedback`
- `cycle 2/3  clean — loop done`

### 33–36s — Stats close
Done box:
- `7 fixes · 1 FP skipped · 2/3 cycles · 0 manual babysitting`
- Followed by `> show Gemini loop stats` repo aggregate (avg cycles, time to
  terminal, findings fixed, judge coverage) — compressed to fit the 3s window;
  if too dense, keep only the done box and fold one aggregate line into it.

## Acceptance

- Total duration 34–38s; last frame holds ≥ 2s.
- Each money frame readable at GIF speed (≥ 2s hold, ≤ 7 lines).
- No line exceeds 96 cols (no wrapping artifacts).
- GIF < 1 MB; README alt text still accurate (update the line under the embed
  to mention repo-aware gate, judge, and stats).

## Out of scope

- No changes to loop scripts or skill docs.
- No real recording — staged cast, real wording.
