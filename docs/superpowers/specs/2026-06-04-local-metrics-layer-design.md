# Local Metrics Layer — Design

**Date:** 2026-06-04
**Status:** Approved (brainstorm), pending implementation plan

## Summary

Add a local metrics layer to the Gemini review loop, delivered as two features over one
shared substrate:

- **Feature 1 — Run summary.** At the end of every loop run, emit a `[loop] Summary`
  block *and* append one structured record to a local append-only log.
- **Feature 2 — Local workflow KPIs.** "Show Gemini loop stats for this repo" reads that
  log and prints aggregate stats for the current repo.

Feature 1 lays the track; Feature 2 rides it. Both are **local-only** and carry **no
identity** — by construction, the data cannot be sliced per developer, so it cannot become
a productivity score.

## Architecture

Script-owned, matching the repo's existing pattern (`judge.py`, `judge_doctor.py` as tested
modules beside `fetch_gemini_threads.py`).

- New tested module **`metrics.py`** owns: the record schema, the append, run-summary
  formatting, and aggregate computation/formatting. Pure logic, no network — fully
  unit-testable.
- **`fetch_gemini_threads.py`** is the single CLI entry point. Two new flags:
  - `--record-run --fixed-count <n> --verification <passed|failed|skipped>` — does a final
    fetch (current thread state), computes derived fields, folds in the agent-supplied
    facts, appends the record, prints the summary.
  - `--stats` (+ `--stats-window N`, `--stats-all-repos`, `--format json`) — read-only
    aggregate for the current repo.
- The agent supplies only the two facts it alone knows (`fixed_count`, `verification`).
  Everything else the script already has or derives.

## Data model & storage

**File:** `runs.jsonl`, beside `state.json` in `~/.config/gh-gemini-review-loop/`, honoring
`GGRL_STATE_DIR`. Append-only, one JSON object per completed run. JSONL so a single corrupt
line never poisons the file and appends need no rewrite.

**Record schema (`schema_version: 1`):**

```json
{
  "schema_version": 1,
  "ts": "2026-06-04T18:22:10Z",
  "repo": "OrenAshkenazy/gh-gemini-review-loop",
  "pr": 23,
  "provider": "gemini-code-assist",
  "findings_fetched": 7,
  "fixed_count": 4,
  "fixed_observed": 4,
  "remaining_actionable": 1,
  "needs_human": 1,
  "addressed_by_reply": 2,
  "cycles_used": 2,
  "cycle_cap": 3,
  "verification": "passed",
  "outcome": "clean",
  "started_at": "2026-06-04T18:10:11Z",
  "duration_seconds": 720,
  "finding_areas": ["tests", "src"],
  "finding_paths": ["tests/test_auth.py", "src/auth/login.py"],
  "judge": { "enabled": false }
}
```

Judge block when judge mode ran:

```json
"judge": {
  "enabled": true,
  "verdicts": {
    "valid_actionable": 3, "false_positive": 1, "duplicate": 1,
    "already_addressed": 1, "explanation_only": 0, "needs_human": 1
  },
  "recommended_actions": { "fix": 3, "reply": 1, "ignore": 2, "escalate": 1 }
}
```

**Field definitions.**

- `findings_fetched` — distinct actionable threads seen during the run.
- `fixed_count` — agent-claimed fixes.
- `fixed_observed` — derived from threads that went UNRESOLVED→RESOLVED/OUTDATED this run.
  Stored alongside the claim so the headline KPI stays honest; Feature 1 displays the claim,
  Feature 2 uses the observed value for its denominators.
- `remaining_actionable` — unresolved findings still potentially fixable at loop end.
- `needs_human` — unresolved threads that should **not** be auto-fixed because they need
  product/design/security/maintainer judgment. **Pending** decisions only.
- `addressed_by_reply` — threads where a maintainer already replied with a substantive
  decision/explanation. A human **already decided** — distinct from `needs_human`.
- `outcome` ∈ `clean | capped | human | regression | no_progress | verification_failed`.
  `verification_failed` is separate from `regression` (env failure, missing dep, flaky test,
  or no verification command available is not a regression).
- `judge` — `{ "enabled": false }` when off; full verdict/action breakdown when on.

**Field ownership.**

- *Script already has:* repo, pr, provider, cycles_used, cycle_cap, judge.*, started_at, ts,
  duration_seconds.
- *Agent supplies:* `fixed_count`, `verification`.
- *Derived by script at record time:* findings_fetched, fixed_observed, remaining_actionable,
  needs_human, addressed_by_reply, finding_areas, finding_paths, outcome.

**Cross-cycle start timestamp.** On the first fetch of a run, the script writes
`loop_started_at` into `state.json` under the existing `owner/repo#number` key. At record
time it reads that back for `started_at` and clears it, so the next loop on the same PR
starts a fresh clock. If `started_at` is missing, set `started_at = ts` and
`duration_seconds = 0`; missing state never blocks the write.

## Feature 1 — Run summary

**Trigger.** Once per run, at loop end (clean, capped, or stopped). Not per cycle.

**Mechanism.** `metrics.py` exposes `record_run(payload) -> Record` (validate, fill derived
fields, append one JSONL line) and `format_run_summary(record) -> str`. At loop end the agent
calls:

```bash
python3 .../fetch_gemini_threads.py --record-run --fixed-count 4 --verification passed
```

`--record-run` does the final fetch, computes derived fields, folds in the flags, appends,
and prints the block.

**Rendered output (judge off — judge lines omitted):**

```text
[loop] Summary
Findings fetched: 7
Fixed: 4
Needs human: 1
Cycles used: 2/3
Verification: passed
Time to clean PR: 12m
```

**With judge on**, two lines insert after "Fixed":

```text
Ignored by judge: 2      # false_positive + duplicate + already_addressed + explanation_only
Needs human (judge): 1   # judge's needs_human verdict, distinct from the loop's line
```

**Omission rules.**
- Judge disabled → both judge lines omitted.
- `addressed_by_reply: 0` → that line omitted.
- "Needs human" line is driven by the **top-level** `needs_human` (loop's pending-decision
  count); the judge's `needs_human` verdict appears only in the judge block / its own line.
- Duration formatted compactly: `48s`, `12m`, `1h 4m`.

## Feature 2 — Local workflow KPIs

**Invocation.** "Show Gemini loop stats for this repo" → `--stats`. Reads `runs.jsonl`,
filters to the current repo (resolved via `gh repo view` / cwd), takes the most recent N
(default 10), aggregates, prints. Read-only; never touches GitHub, never writes.
`metrics.py` provides `aggregate(records) -> Stats` and `format_stats(stats) -> str`.

**Output (judge ran on some runs):**

```text
Gemini loop stats — OrenAshkenazy/gh-gemini-review-loop
Last 10 runs

Average cycles used: 1.8
Average time to clean PR: 9m
Findings fixed: 32 of 41
Human decisions needed: 6
Addressed by reply: 9
False positives avoided: 14   (across 6 of 10 judged runs)
Most common provider: gemini-code-assist
Most repeated finding area: tests
```

**Mapping to records.**
- Average cycles → mean of `cycles_used`.
- Average time → mean `duration_seconds`; runs with `duration_seconds: 0` excluded from the
  average; formatted compact.
- Findings fixed: X of Y → Σ`fixed_observed` of Σ`findings_fetched` (observed denominator).
- Human decisions needed → Σ top-level `needs_human`.
- Addressed by reply → Σ`addressed_by_reply`.
- False positives avoided → Σ`judge.verdicts.false_positive`, **only over runs where
  `judge.enabled`**, with coverage footnote (`across 6 of 10 judged runs`). Zero judged runs
  → line omitted.
- Most common provider → mode of `provider`.
- Most repeated finding area → mode of flattened `finding_areas`.

**Defaults & flags.** Repo-scoped by default; `--stats-all-repos` to opt out. `--stats-window
N` (default 10). `--format json` for machine consumption.

**Empty / sparse state.** No `runs.jsonl` or zero matching runs → friendly one-liner, not an
error:

```text
No Gemini loop runs recorded yet for this repo. Run the loop once and stats will appear here.
```

Fewer runs than the window → "Last 4 runs" (actual count), no padding.

## No productivity scoring (design constraint)

- Counts and averages only. No per-developer attribution, no rate/velocity score, no
  rankings, leaderboards, grades, or trend lines that invite "are you improving?" readings.
- Every number is a plain workflow fact framed around the PR review workflow, never the
  person.
- **No identity is recorded** — records carry repo/PR, never a git author or GitHub login.
  This is the enforcement: scoring people is structurally impossible, not merely discouraged.

## Error handling

Metrics are the last, strictly-additive step and must never break the loop.

- `runs.jsonl` unwritable → stderr `warning: could not record run metrics: <reason>`; loop
  still reports DONE. Same posture as existing GraphQL page-limit warnings.
- Corrupt line in `runs.jsonl` → `--stats` skips it and footnotes `(1 unreadable record
  skipped)`; never aborts. (The reason for JSONL over a single array.)
- Missing `started_at` → fallback as above.
- Append is atomic-enough for the single-user local case: open `"a"`, one `write()` of
  `json.dumps(record) + "\n"`. No lock file — concurrent loops on the same machine are out
  of scope (YAGNI).

**Schema versioning.** Every record carries `schema_version`. `--stats` reads understood
versions, skips future versions (counted footnote). Aggregation reads fields defensively
(`.get(...)` with defaults) so old records survive new fields — what makes the `finding_paths`
"store now, use later" bet safe.

## Testing

Matches the existing pytest layout under `tests/`.

- **`metrics.py` unit tests (bulk, no network):**
  - `record_run`: schema shape, derived fields, judge-on vs judge-off block, `fixed_observed`
    from transitions, missing-`started_at` fallback, all six `outcome` values.
  - `format_run_summary`: judge-off omits judge lines; `addressed_by_reply: 0` omits line;
    duration formatting (`48s` / `12m` / `1h 4m`).
  - `aggregate` / `format_stats`: averages, `X of Y` fixed, false-positives-only-over-judged
    runs + coverage footnote, modes for provider/area, empty state, sub-window count,
    corrupt-line skip, future-version skip.
- **`fetch_gemini_threads.py` integration:** `--record-run` writes exactly one well-formed
  line folding in `--fixed-count`/`--verification`; a record-write failure does not raise.
- **Fixtures:** a small `runs.jsonl` with judged + unjudged + one corrupt + one future-version
  line to drive aggregate tests.

## Skill & docs wiring

**SKILL.md.**
1. New "Run Metrics" section: per-run record (what/where, `GGRL_STATE_DIR`), the `--record-run`
   contract, and the "no productivity scoring / no identity recorded" stance. Cross-link the
   `state.json` and judge sections.
2. Workflow step 10 gains a closing sub-step: on terminal state, call `--record-run
   --fixed-count <n> --verification <...>` exactly once, then emit the summary. The agent
   already tracks both facts through narration.
3. Progress Narration table — new final row: `Loop complete / stopped | [loop] Summary block`.
   Follows the existing `DONE`/`STOP` line.
4. Variations table — new row: `Local stats | "show Gemini loop stats" / "loop stats for this
   repo" | --stats`.
5. One guardrail line near Variations: stats are local-only, never posted to GitHub.

**README.** A "Run metrics & local stats" subsection after the judge material: both example
outputs, the local-only/no-scoring promise, and a note that judge-derived lines appear only
when judge mode is on.

**PRIVACY.md.** One sentence: metrics are stored locally under
`~/.config/gh-gemini-review-loop/`, contain no identity, and are never transmitted.

## Out of scope (YAGNI)

No retention/rotation policy, no `--clear-stats`, no per-developer slicing, no cross-machine
sync, no concurrent-loop locking. Add later only if asked.
