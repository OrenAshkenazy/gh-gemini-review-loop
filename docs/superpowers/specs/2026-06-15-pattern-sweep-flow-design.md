# Pattern → Sweep → Converge Flow — Design

**Date:** 2026-06-15
**Status:** Approved (design), pending implementation plan
**Branch:** `feat/pattern-sweep-flow`

## Problem

Gemini Code Assist is an LLM reviewer. When it flags a code pattern (e.g. "validate
this value's type before using it"), fixing the flagged sites teaches it the repo
wants that pattern — so on the next re-review it flags *more* instances of the same
pattern in other changed files it hadn't reached. The finding count does not decay;
it expands one file per cycle until the loop hits its re-review cap.

Observed on PR #46: per-cycle new findings ran `6 → 4 → 9 → 5 → 3 → 5` (32 total),
hit the 5/5 cap, 33 minutes, 15 findings still open. The judge confirmed all 32 were
`valid_actionable`, 0 false-positive, 0 needs-human — so this was not noise in the
"wrong findings" sense. It was **scope expansion on a consistent pattern**: the same
suggestion shape, rediscovered file by file.

The current loop treats N instances of one pattern as N independent findings, fixes
only the flagged sites each cycle, and gives no signal explaining why the count won't
reach zero.

## Goal

Collapse the expansion into a single cycle. Concretely:

1. **Cluster** findings by pattern so the user reasons about *kinds of issue*, not a
   flat list.
2. **Sweep** each multi-site pattern across the PR's changed files — fixing sibling
   instances Gemini has not flagged yet — so the next re-review has nothing left to
   generalize to.
3. **Detect convergence**: track pattern recurrence across cycles and surface an
   advisory when a pattern reappears after being swept (the sweep missed a variant,
   or Gemini keeps re-flagging).

Non-goals (explicitly out of scope for this spec): severity-gated auto-fixing, a
noise-metrics dashboard, LLM-based clustering, repo-wide sweeping, and any change to
loop control flow (the re-review cap remains the only hard stop).

## Design decisions (settled during brainstorming)

| Fork | Decision | Rationale |
|---|---|---|
| Pattern detection | **Heuristic / deterministic** | Zero cost, no network, fully unit-testable. The observed noise was literally the same suggestion shape repeated, so text normalization catches it. LLM clustering can come later if paraphrase defeats the heuristic. |
| Sweep scope | **Changed files only** | Both safe (blast radius = the PR's own diff surface, no scope creep) and *sufficient* — Gemini only reviews changed files, so this covers everything it can generalize to. |
| Convergence action | **Advisory only** | Surface a recommendation; never auto-change control flow. Fits the plugin's "deterministic block, agent/user decides" model. The cap stays the hard backstop. |
| Sweep autonomy | **Report-then-go** | Print the sweep report (which unflagged sites, why) then fix in the same cycle without blocking — same trust model as today's per-cycle fixes, made visible. User can interrupt; no silent edits to unflagged code. |

## Architecture

One new pure module does the analysis; existing scripts get thin wiring. No network,
no LLM. All logic is deterministic and unit-testable, matching the codebase style.

### New module: `cluster_findings.py`

Lives in
`plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/cluster_findings.py`.
Pure functions, `from __future__ import annotations`, stdlib only.

```python
def pattern_signature(thread: dict[str, Any]) -> str:
    """Pattern skeleton of a finding, stable across paraphrase and location.

    Distinct from finding_fingerprint(): that keeps path + text to identify ONE
    finding; this strips everything location- and instance-specific to capture
    the KIND of suggestion, so two findings of the same kind in different files
    share a signature.
    """
```

Normalization pipeline (applied to the first comment body):

1. Drop the severity image markdown (`![high](...)`), reusing the existing regex.
2. Drop fenced code blocks, especially ` ```suggestion ` blocks (file-specific code).
3. Drop inline code spans (`` `foo` ``) — these carry instance-specific identifiers.
4. Drop line/column number echoes (`line 336`, `:336`, `L336`).
5. Drop numeric and quoted-string literals (replace with a placeholder token).
6. Lowercase, collapse whitespace, trim.
7. Hash the first N chars of the result (mirror `finding_fingerprint`'s `[:1000]`/sha
   approach for consistency).

What survives is the natural-language description of the issue ("validate that the
value is a dict before calling get"), which is the pattern.

```python
@dataclass(frozen=True)
class Cluster:
    signature: str
    label: str                  # short human title from the most common normalized phrase
    severity: str               # max severity across members (critical>high>medium>low>unknown)
    sites: list[str]            # ["path:line", ...] for member findings
    count: int

def cluster(threads: list[dict[str, Any]]) -> list[Cluster]:
    """Group threads by pattern_signature. Sorted by severity desc, then count desc."""
```

`label` derivation (v1): take the normalized body of the cluster's
highest-severity member, truncate to a short phrase. Deterministic, good enough to
scan; not required to be perfect prose.

### Sweep: a SKILL.md-orchestrated step, not a code edit

The script cannot edit code — the agent does. The script's job is to hand the agent
everything needed to sweep:

- the cluster `label` (what the pattern is),
- the member `sites` (concrete examples of the code shape),
- the list of changed files (from the existing `changed_files_in_range()` /
  `gh pr` data the loop already has).

SKILL.md gains a documented step: for each multi-site cluster (`count >= 2`), grep the
**changed files** for sibling instances of the same shape, then fix the whole cluster
**plus** the swept siblings in one cycle. The agent reports the sweep before editing
(report-then-go) and marks completion with new/existing flags (below).

Rejected alternative: have the script auto-generate a grep regex from the NL
suggestion. Brittle — deriving a reliable code pattern from prose is unreliable.
Examples-driven agent grep (the agent sees real member sites and greps for that shape)
is more robust and matches how the agent already works.

### Convergence: advisory metric

State tracking parallels the existing `seen_finding_fps` machinery in
`fetch_gemini_threads.py`, but at pattern granularity:

- `seen_pattern_sigs` / `prior_seen_pattern_sigs` — running union of pattern
  signatures, snapshotted before folding in the current cycle (mirror of
  `track_finding_fingerprints`).
- `swept_patterns` — set of signatures the agent has marked swept, accumulated via a
  new CLI flag `--swept-pattern <sig>` (parallels the existing `--fixed-finding`).

Derived signals (computed in `cluster_findings.py` or `metrics.py`):

- `pattern_recurrence_rate` = (this cycle's findings whose signature was seen in a
  prior cycle) ÷ (this cycle's findings). Range 0.0–1.0.
- **recurred-after-sweep**: a signature present in `swept_patterns` reappears in the
  current cycle's findings → emit a warning line and tailor Next options.

Advisory only: never changes control flow. The re-review cap remains the hard stop.

### Receipt rendering

The `--cycle-summary` receipt (built in `metrics.py`, relayed verbatim by the agent)
gains a `Patterns (N):` section above the existing `Findings (N):` list:

```
Patterns (2):
  [HIGH]   tab-vs-space indent detection — 1 site            (sig: a1b2c3d4)
           mergeproof_config.py:68
  [medium] missing isinstance guard before .get/.str — 8 sites (sig: e5f6a7b8)
           render_pr_readiness.py:336, render_demo_ui.py:204, +6 more
```

Each cluster line shows its short signature token (`sig: …`). That token is what
the agent echoes back as `--swept-pattern <sig>` after sweeping the pattern, so the
recurrence detector can match a swept pattern against a later cycle's findings. The
signature is the only piece of machine state the agent passes by hand; everything
else (sites, severity, recurrence) is derived by the script.

Plus a `Convergence:` line:

```
Convergence: 2 distinct patterns this cycle, 0 recurred. Swept 1 pattern.
```

and, when a swept pattern recurs:

```
Convergence: ⚠ pattern "missing isinstance guard" RECURRED after sweep.
             Sweep missed a variant or Gemini keeps re-flagging.
```

The existing `Findings (N):` list is unchanged — the canonical per-finding list with
URLs stays. Patterns sit on top of it.

### Terminal record (`runs.jsonl`)

`--record-run` gains an additive `patterns` block (back-compatible; absent on old
records):

```json
"patterns": {
  "distinct_patterns": 4,
  "max_cluster_size": 14,
  "pattern_recurrence_rate": 0.0,
  "swept_count": 1
}
```

## Per-cycle data flow

1. fetch threads → actionable list (unchanged)
2. **cluster** → patterns (new)
3. agent: for each multi-site cluster, **sweep** changed files for siblings, report,
   fix cluster + siblings together; pass `--fixed-finding <fp>...` and
   `--swept-pattern <sig>`
4. verify (pytest profile, unchanged)
5. `--cycle-summary` → receipt now carries `Patterns:` + `Convergence:` (new)
6. push, re-review, loop

## Surfaces touched

- **NEW** `scripts/cluster_findings.py` — `pattern_signature`, `Cluster`, `cluster`,
  recurrence helpers.
- **NEW** `tests/test_cluster_findings.py` — fixtures from real PR #46 threads.
- `scripts/metrics.py` — `patterns` block in the terminal record; `Patterns:` /
  `Convergence:` receipt rendering.
- `scripts/fetch_gemini_threads.py` — pattern-sig state tracking (parallel to
  `seen_finding_fps`), `--swept-pattern` flag, wire `cluster()` into `--cycle-summary`
  and `--record-run`.
- `skills/gh-gemini-review-loop/SKILL.md` — document the 5-step flow + new flag.

## Error handling

Mirror the codebase's fail-open discipline for the local correctness aids:

- `pattern_signature` on a malformed thread (non-dict, missing comments) returns a
  stable empty/degenerate signature rather than raising — clustering must never crash
  the loop.
- Pattern-sig state I/O failures fail open (empty prior → everything reads as a new
  pattern), exactly like `track_finding_fingerprints`.
- The `Patterns:` / `Convergence:` receipt sections are additive; if clustering yields
  nothing useful, the receipt degrades to today's `Findings:`-only output.

## Testing

Pure functions → unit tests, no network, matching existing `tests/` style
(`sys.path` wired by `tests/conftest.py`; run via `/opt/homebrew/bin/pytest`).

Key assertions:

1. **Signature stability**: two findings describing the same issue with different
   identifiers / paths / line numbers share a `pattern_signature`.
2. **Signature separation**: genuinely different issues (the HIGH tab-detection bug vs
   a type-guard finding) get different signatures.
3. **Clustering**: `cluster()` groups correctly, picks max severity, sorts by severity
   then count, lists sites.
4. **Recurrence math**: `pattern_recurrence_rate` and the recurred-after-sweep flag
   compute correctly across simulated cycles.
5. **Fail-open**: malformed threads don't raise.

Fixtures: capture a handful of real PR #46 thread JSON payloads (the type-guard
cluster + the lone HIGH) as the golden corpus.

## Expected impact (PR #46 replayed)

| | Today | New flow |
|---|---|---|
| Things to reason about (cycle 2) | 9 findings | 2 patterns |
| Sites fixed per sweep cycle | 9 (flagged only) | ~15 (flagged + swept siblings) |
| Cycles to converge | 5 (hit cap) | likely 2 |
| Findings open at end | 15 | ~0–2 |
| "Why won't it stop?" | invisible | explicit convergence advisory |
