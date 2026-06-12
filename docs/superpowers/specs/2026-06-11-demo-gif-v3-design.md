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

### 27–30s — Done box
- `7 fixes · 1 FP skipped · 2/3 cycles · 0 manual babysitting`
- `every cycle gated by uv run pytest — regression-proof`

### 30–36s — Audit trail table (hold ~3s)
Three-column box table tracing every finding to its commit:
`thread | originally flagged | fixed in` — 7 fixed rows (5 medium → `5330451`,
2 high → `d04a50c`) plus the FP row marked `reply`.

### 36–45s — Stats close (hold ~5s)
`> show Gemini loop stats` → full `--stats` block (last 10 runs): avg cycles,
elapsed-to-terminal split by outcome (clean/capped/failed), active cycle time,
findings fixed 32/41, human decisions, addressed-by-reply, FPs avoided 14,
provider, hottest finding area.

## Staging note: clear-screen chapters

Content (~74 rows) exceeds terminal height; scrolling makes every line a
full-frame GIF diff (2.8 MB). Instead each beat clears the screen (`ESC[2J`)
and plays on its own 86×22 canvas — punchier chapters and ~270 KB output.

## Acceptance

- Total duration 40–50s; last frame holds ≥ 3s.
- Each money frame readable at GIF speed (≥ 2s hold) and fits one screen.
- No line exceeds 86 cols (no wrapping artifacts).
- GIF < 1 MB; README alt text still accurate (update the line under the embed
  to mention repo-aware gate, judge, audit trail, and stats).

## Out of scope

- No changes to loop scripts or skill docs.
- No real recording — staged cast, real wording.
