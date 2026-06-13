# Chunked Wait Heartbeats — Design

**Date:** 2026-06-11
**Status:** Approved direction; spec for implementation planning
**Problem:** During the loop's Gemini wait (1–5 min), the orchestrating agent
backgrounds the blocking `--wait` call. The session looks idle: no progress in
chat, script progress lines go to the stderr of a detached process. Observed
live on AegisLocal PR #12 — the loop was healthy, Gemini had already responded,
and the user could not tell.

## Solution shape

Turn one long opaque wait into a sequence of short **foreground** wait chunks.
Each chunk either completes the wait (`ready`) or returns a deterministic
script-owned status that the agent relays verbatim as a purple `[loop]`
heartbeat, then immediately starts the next chunk. Heartbeats land in chat
every 60–90s, well inside the 5-minute prompt-cache TTL, so each one is a
cheap cache-read turn.

Background waits are removed from the orchestration. The wait state machine
lives in the script; the agent only loops and relays.

## CLI surface (`fetch_gemini_threads.py`)

- `--wait-chunk-seconds <int>` — new, optional, only meaningful with `--wait`.
  Maximum seconds this process may block. Omitted → exact legacy blocking
  behavior (no breaking change). Name deliberately distinct from `--timeout`:

  ```text
  --wait-chunk-seconds = how long this process may block
  --timeout            = total wait budget across all chunks (default 900)
  ```

- `--wait-heartbeat` — new formatter-only command (same family as
  `--profile-intro`). Reads the persisted wait state for the PR and prints the
  deterministic human heartbeat block. Used by the agent after a JSON-mode
  pending chunk, so JSON stdout stays machine-only and the human block still
  comes from the script, not agent prose.

## Wait statuses

Four real states, not two — the existing quiet-period concept makes
"Gemini responded but not yet stable" a distinct user-visible phase:

```text
waiting   = no Gemini activity after --after yet
settling  = activity detected, quiet period not yet complete (cycle 2+ only)
ready     = activity present and stable → proceed to fetch (legacy success)
timed_out = cumulative budget exceeded → feeds --gemini-unconfirmed terminal path
```

`ready` behaves exactly like today: the same invocation continues into
fetch/markdown/JSON output. `timed_out` is a status, not a crash: exit 0 with
explicit status so the agent records the run with `--gemini-unconfirmed`
(existing PR #40 flag pair; kept as-is) and allows `fixed_pending_confirmation`.

## Cross-chunk state (sticky state, per-PR `run` entry)

A chunk process dies between heartbeats, so all wait progress persists in the
existing sticky state, keyed like `seen_finding_fps`:

```text
wait_after              ISO anchor this wait is bound to (None for cycle 1)
wait_started_at         wall-clock ISO, set on first chunk of this wait
wait_checks             chunk invocation count (increments once per chunk)
wait_stable_fingerprint last seen activity fingerprint (settling)
wait_stable_since       wall-clock ISO when that fingerprint was first seen
```

**Reset rule (prevents cross-cycle leakage):**

```text
if stored wait_after != current --after:
    reset wait_started_at, wait_checks,
          wait_stable_fingerprint, wait_stable_since
    store new wait_after
```

**Settling must survive chunk boundaries.** `stable_since` /
`last_fingerprint` are currently process-local monotonic values. With 60–90s
chunks and a 45s quiet period, a chunk can end mid-settle; without persisted
`wait_stable_fingerprint` + `wait_stable_since` the next chunk would restart
the quiet period and settling could ping-pong indefinitely. Persisting both is
a correctness requirement, not an optimization. Within a single chunk the
implementation may keep using monotonic time; the persisted wall-clock values
are the cross-chunk source of truth.

State is cleared on `ready` and by existing run-tracking cleanup
(`clear_run_tracking`).

## Timeout enforcement

Cumulative `--timeout` (default 900s) is enforced against elapsed computed as:

```text
elapsed = max(now - wait_started_at, now - wait_after)   # cycle 2+
elapsed = now - wait_started_at                          # cycle 1 (no --after)
```

The `--after` floor makes timeout enforcement robust to state loss: if the
sticky state is corrupt or deleted, a fresh `wait_started_at` cannot silently
restart the budget — `now - after` still bounds the total wait. Fail-open
remains the policy for heartbeat *display* (a lost state undercounts elapsed
in the heartbeat text); the floor closes the *enforcement* hole. Cycle 1 has
no anchor, which is acceptable: the cycle-1 fast path returns on first
detected activity, so chunking barely engages there.

Clock skew between GitHub timestamps (`--after`) and local wall clock is
tolerated; the floor only needs to be roughly right to prevent unbounded
waiting.

## Output discipline

```text
stdout (markdown mode) = human blocks — heartbeat printed purple, relay verbatim
stdout (--format json) = machine JSON only — never colored, never mixed
stderr                 = diagnostics only — never the user-visible heartbeat path
```

- Markdown-mode pending chunk, exit 0:

  ```text
  [loop] waiting for gemini-code-assist — 90s elapsed, 2 checks done, next check in 90s
  ```

  Settling variant:

  ```text
  [loop] Gemini responded — waiting for review threads to settle, 30s quiet period remaining
  ```

- JSON-mode pending chunk, exit 0:

  ```json
  {
    "wait": {
      "status": "settling",
      "elapsed_seconds": 120,
      "checks": 3,
      "submitted_at": "2026-06-11T12:04:27Z",
      "quiet_period_remaining_seconds": 30,
      "next_wait_seconds": 30
    }
  }
  ```

  The agent then runs `--wait-heartbeat` to render the human block for relay.

- Rendering goes through `metrics.format_wait_heartbeat(...)` — a pure
  formatter with unit tests, like the PR #40 formatter family. Color via
  `loop_color` on `[loop]`-prefixed blocks only.

## Decay schedule

Script-computed, agent-followed: `next_wait_seconds` is 60 for the first
chunk, 90 thereafter (derived from `wait_checks`; settling may suggest the
remaining quiet period when smaller). SKILL.md instructs: first call uses
`--wait-chunk-seconds 60`, every later call passes the script's suggested
value. All gaps stay far below the 5-minute cache TTL.

## SKILL.md changes

- **Never background the wait.** Run chunked foreground waits in a loop.
- After each non-`ready` chunk: relay the heartbeat block verbatim (markdown
  mode: from chunk stdout; JSON mode: from `--wait-heartbeat`), then
  immediately run the next chunk with the suggested `--wait-chunk-seconds`.
- On `timed_out` at a terminal cycle: record with `--gemini-unconfirmed`; do
  not guess `clean`; allow `fixed_pending_confirmation` (existing rules).
- Narration table gains one row for the heartbeat relay.

## Explicitly rejected

- `--gemini-confirmed false` value-style flag: the shipped PR #40 interface is
  a `store_true`/`store_false` pair (`--gemini-confirmed` /
  `--gemini-unconfirmed`); switching to a value-taking arg would break a
  just-shipped interface for no behavioral gain.
- Heartbeat on stderr in JSON mode: stderr is diagnostics, not a user-visible
  channel.
- Background wait + harness polling (Monitor/TaskOutput): harness-specific,
  agent-discretionary timing — the opposite of deterministic narration.

## Testing

- `format_wait_heartbeat` unit tests: waiting/settling wording, counts,
  pluralization, zero/corrupt inputs.
- Chunk-state tests: accumulation across chunks; reset on `--after` change;
  settling survives a chunk boundary (quiet period does not restart);
  `ready`/cleanup clears state.
- Timeout-floor tests: corrupt/missing state still times out via
  `now - after`; cycle-1 path without anchor.
- Legacy test: no `--wait-chunk-seconds` → blocking behavior byte-identical.
- JSON stdout discipline tests extended to pending/settling/timed-out chunks
  and `--wait-heartbeat`.
