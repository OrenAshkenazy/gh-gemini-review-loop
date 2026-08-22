---
name: gh-review-loop
description: Use after a GitHub PR is opened, or when the user asks to handle AI reviewer feedback from Codex, CodeRabbit, Copilot, or another configured reviewer bot, run the review loop, fix reviewer comments, sweep sibling instances of a flagged pattern, or request re-review. Waits, fixes, verifies against the repo's own tests, pushes, re-asks. Capped by user preference, default 3 cycles.
---

# AI Reviewer PR Review Loop

Run the full GitHub PR loop: after PR creation, wait for the configured AI reviewer, fetch unresolved actionable review threads, acknowledge the requested fixes, implement them, verify against the repo's own checks, push, and request re-review — capped.

Prefer thread-aware review data over flat PR comments: review threads preserve `isResolved`, `isOutdated`, file paths, line anchors, and diff hunks.

## Reference files (load on demand)

This file holds the default-path rules. Deeper material lives in `references/` next to this file — Read the referenced file **at the point of need**, not up front:

| File | Load when |
|---|---|
| `references/sweep-internals.md` | The cycle receipt shows a multi-site pattern (`count >= 2`), a multi-line ranged finding, or a `Clustering:` singleton advisory — before running or interpreting `sweep_siblings.py` |
| `references/judge-eval.md` | `judge_mode` ≠ `off` in prefs, the user mentions judge/eval, or the one-time judge tip is due |
| `references/receipts-and-metrics.md` | Posting a PR receipt (`--post-receipt`/`--sticky-receipt`), passing `--semantic-risk`, running `--stats`, or asked what run metrics store |
| `references/terminal-report.md` | A terminal `[loop] Summary` has `remaining_actionable > 0` — before writing the closing report |
| `references/resume-and-recovery.md` | Re-invoking the loop on a PR already at the cap, or new pushes land after the loop stopped |
| `references/variations.md` | The user's phrasing isn't the plain default loop (severity filters, audit-only, cap changes, reviewer switch, judge modes, stats, profile management) — authoritative phrasing → flag table |
| `references/script-usage.md` | Needing a script invocation not shown inline here (history investigation, reviewer discovery/reset, read-only fetch, dry-run, JSON output) |

## Reviewer Selection

One configured reviewer bot per PR. First run with no persisted reviewer is prompt-first:

1. Run `fetch_gemini_threads.py --list-reviewers --format json`.
2. If candidates are returned, ask the user to confirm/choose, then persist with
   `--reviewer <login> --reviewer-source confirmed [--reviewer-name <name>] [--review-trigger-mention <mention>]`.
3. Zero candidates on a fresh PR → the selection returns `source: "none_configured"`, `configured: false`. Do not treat the accompanying `suggestion` as a choice already made. Offer: **Ping the suggested reviewer** (`@codex review` — reviews only on request, so accepting starts cycle 0), **Pick another reviewer** (any known vendor, including Gemini Code Assist, or a bot whose login + mention the user supplies), or **None** (stop with "No AI reviewer threads found on this PR."). Never wait on a reviewer the user did not choose — an uninstalled reviewer is indistinguishable from a slow one.
4. `partial: true` → do not claim no reviewer exists; ask the user or retry discovery.

Returning runs reuse the persisted reviewer silently. Switch with `--reviewer`; rediscover with `--reset-reviewer`.

When a JSON fetch reports `reviewerSelection.confirmation_required: true`, stop before edits or re-review requests and run the prompt flow. `source: default_unconfirmed` is not a confirmed selection.

### Known reviewers

| Reviewer | Login | Trigger posted | Priority format | Reviews unprompted |
|---|---|---|---|---|
| Gemini Code Assist | `gemini-code-assist` | `@gemini-code-assist please review the latest changes.` | `![high]` alt text | yes |
| Codex | `chatgpt-codex-connector` | exactly `@codex review` | `![P0]`–`![P3]` badges | **no** |

Codex priorities normalize to the shared scale (P0→critical … P3→low), so `--min-severity` works unchanged. Codex may put findings in the review body with no inline thread; the fetcher surfaces those and drops them when a newer Codex review supersedes them. Gemini's consumer app shut down 2026-07-17 — if a user picks Gemini and no threads ever arrive, say that instead of waiting out the timeout. Other discovered bots work, but the loop only re-asks them when a safe mention is supplied via `--review-trigger-mention`; never guess `@login` for an unknown bot.

## Thread States

- **`RESOLVED`** — explicitly resolved. Skip.
- **`OUTDATED`** — line anchor moved out from under the thread. Auto-resolved by the script. Skip.
- **`ADDRESSED_BY_REPLY`** — unresolved, but a maintainer posted a substantive reply (≥30 chars, non-bot, not a token ack). A human decision to defer — do not fix again. Auto-resolved on the next pass (opt out: `--no-resolve-addressed-by-reply`).
- **`UNRESOLVED`** — actionable. Drives the next fix attempt.

## Cycle Counting

A **cycle** is one re-review request posted by the agent after the reviewer's first review.

- **Cycle 0:** the reviewer's first review (automatic for self-starting bots, or the agent's opening ping for ask-only bots). Free.
- **Cycles 1–N:** each subsequent agent-posted re-review request; `N` = `max_rereview_requests` (prefs file, default 3). After cycle N, hard stop.
- Replies and pushes without a re-review request do NOT count. Only re-reviews posted by the agent itself count (login auto-detected via `gh api user`; `--agent-login NAME` / `--no-agent-filter` to override).

The cap blocks new re-review requests and new fix cycles. It does NOT block stale-thread cleanup, addressed-by-reply cleanup, metrics, terminal classification, or recording terminal state.

## Severity Ordering

Actionable threads are ordered `critical → high → medium → low → unknown`, parsed from the reviewer's markdown image alt text (`![high]`, or Codex `![P0]`–`![P3]`). Other bots' findings carry `unknown` severity.

## Pattern → Sweep → Converge

Bot reviewers are LLMs: fixing only the flagged sites of a pattern teaches the bot to flag more instances next cycle. Collapse that into one cycle — **Finding → Pattern → Sweep → Verify → Re-review**:

1. **Cluster.** The cycle receipt's `Patterns (N):` section groups findings by a deterministic pattern signature. Reason about patterns, not a flat finding list.
2. **Sweep (report-then-go).** For each multi-site pattern (`count >= 2`) and each single finding anchored to a multi-line range: load `references/sweep-internals.md`, run `sweep_siblings.py` over the PR's **changed files only**, print the sweep report, then fix the cluster plus the reported siblings this cycle. Never edit unflagged code silently — the report must appear first. Honor the script's `status`: `too_few_sites`, `pattern_too_thin`, `no_source` mean do not sweep; `truncated: true` means show the report and ask before touching unflagged code.
3. **Mark.** Pass `--swept-pattern <sig>` (the `sig:` token from the Patterns receipt) for each swept pattern, alongside `--fixed-finding` markers.
4. **Verify** (repo profile) and **re-review** as usual.

The receipt's `Clustering:` and `Convergence:` lines are advisory. Three or more singleton clusters → `Clustering:` warns the signatures are likely prose-hash fallbacks and requires the manual sweep procedure in `references/sweep-internals.md` before fixing. A `⚠ … RECURRED after sweep` means the sweep missed a variant — decide whether to refine, stop, or continue. Neither line changes control flow; the cap remains the only hard stop.

## Verification Profile

Each repo can have a code-derived **verification profile** — the checks the verify step runs. Stored in `~/.config/gh-review-loop/preferences.json` under `profiles["owner/repo"]`.

**First run** (no profile for the repo yet) — after fetching findings, **before the first fix attempt** (a `PreToolUse` hook blocks edits until a profile decision is saved):

1. Run `detect_profile.py <repo_root>` → `{stack, confidence, reasons, candidate_checks, presets}`. `presets` is the code-built option list — do not hand-roll the menu.
2. `stack == "unknown"` → do not prompt, but still **record the decision**: `judge.save_profile(repo, source="skipped", detected_stack="unknown")`, then use ad-hoc verification. The marker is what distinguishes "decided: ad hoc" from "not decided yet" — without it the edit gate never clears and every later fetch re-reports a first run.
3. Reconcile against repo docs (`CLAUDE.md`, `CONTRIBUTING`, `README`). If docs pin a non-standard invocation, surface it as a note beside the menu; never auto-persist an absolute path from prose.
4. Prompt once, using each `presets[i].label` verbatim as an option.
5. Persist via `judge.save_profile(...)`: `customize == true` → free-form customize path, `source="customized"`; otherwise persist `preset["checks"]` with `source=preset["source"]`. Every persisted check is `required: true`.
6. The fetch that preceded this decision could not describe a profile that did not exist, so it emitted a regenerate notice instead of the usual blocks (`humanBlocks.profileBlocksProvisional: true`, empty `plannedVerification`). Do not relay that notice as the intro — regenerate both blocks from the saved profile with `--profile-intro --repo <owner/repo>` and `--planned-verification --repo <owner/repo>`, and relay those.

**Subsequent runs** — a profile (even `skipped`) exists → no prompt. The fetch output ends with the profile intro and planned-verification blocks — relay the intro from there (no separate `--profile-intro` call), then for `confirmed`/`customized` run `run_profile.py <owner/repo> <repo_root>`; feed its `verification` field into `--verification` and its JSON into `--verification-details`. On `skipped`/unknown, relay the fallback intro and use ad-hoc narrowest-meaningful checks.

**Gate semantics.** Verify fails iff any `required` check fails or times out. Before running checks, relay the planned-verification block from the fetch output — except on a first run, where the fetch predates the profile and carries no suite, so regenerate it with `--planned-verification --repo <owner/repo>` after saving. Route all verification through `run_profile.py` when a profile is confirmed — never call the test runner directly; the runner times checks, captures structured output, and sets the exit code.

**Customizing / un-skipping.** `skipped` suppresses automatic prompts only. Explicit user intent overrides: "add mypy to the checks" → `save_profile(..., source="customized")`; "set up a verification profile" → re-run detect → menu → save even over a `skipped` marker.

## Receipts: per-cycle and terminal

- **`--cycle-summary`** — read-only mid-loop receipt. Builds from the accumulated run state. Does not write `runs.jsonl`, does not clear the accumulator. Safe every cycle.
- **`--record-run`** — terminal. Fetches current thread state, appends one record to local `runs.jsonl`, and **clears the run accumulator**. Call exactly once, at loop end. Key flags: `--fixed-count`, `--verification <passed|failed|skipped>`, `--verification-details '<json>'`, `--outcome <clean|capped|human|regression|no_progress|verification_failed|fixed_pending_confirmation>`, `--outcome-reason '<text>'`, `--gemini-confirmed`/`--gemini-unconfirmed`, `--fixed-finding <fp>` (repeatable), `--swept-pattern <sig>` (repeatable).

**Single-channel delivery.** Both commands deliver the FULL receipt (verification suite, findings list with URLs, severity breakdown) to the sticky PR comment — one comment per PR, edited in place — and print a one-line `[loop]` pointer to stdout: counts + verification + link to the receipt comment. Relay that pointer line verbatim in your text response; do NOT reprint the full receipt in chat. If the comment write fails (or `--dry-run`), the script falls back to printing the full receipt on stdout — relay that fallback verbatim instead, so the receipt is never lost.

**Emit a receipt at the end of every cycle.** Non-terminal cycle → `--cycle-summary` right after verify, REQUIRED even when fixes were small. Terminal cycle → `--record-run` only (never both on the same cycle; never `--record-run` twice).

**Script-owned human blocks** — relay verbatim, never paraphrase unless the script fails: profile intro, planned verification, judge table/skip line, the receipt pointer line (or fallback receipt), semantic-risk note, `Next options:`, wait heartbeat. Before the reviewer confirms a re-review, use the script's fixed-pending wording (`Fixed locally` / `Awaiting push/re-review confirmation`), never `Remaining valid actionable`. Keep the script's ANSI color in human-readable `[loop]` output; `--json`/`--format json` stdout is machine JSON only — parse it, do not relay it unless asked.

## Progress Narration

<HARD-GATE>
DO NOT run `git push` or call `--record-run` until you have:
1. Called `--cycle-summary` (for a non-terminal cycle) OR confirmed `--record-run` is the terminal call.
2. For a terminal cycle after a final push, requested reviewer re-review,
   captured `REREVIEW_AT`, waited with `--wait --after "$REREVIEW_AT"`, and
   set the terminal record's reviewer confirmation flag from that wait result.
3. Relayed that call's printed `[loop]` receipt pointer line (or, on the
   stdout fallback, its full receipt output) in your text response to the user.

This is enforced mechanically: a PreToolUse:Bash hook (`loop_summary_gate.py`) blocks every `git push` while a review loop is active and the summary is stale. The hook does NOT fire for `--record-run` (the terminal receipt is exempt). Trying to push without summarizing first returns exit code 2 and explains the fix.

Violating the letter of this rule violates the spirit.
</HARD-GATE>

Emit one-line status updates at each phase transition:

| Phase | Narration line |
|---|---|
| Before script fetch | `[loop] session cycle N — re-review cap: M/K consumed. Fetching threads from PR #<num>...` (N = session-local cycle, M = re-reviews already on the PR, K = cap — always show both) |
| After fetch, before fixes | `[loop] session cycle N — <K> actionable thread(s) (severity: <breakdown>). Fixing.` + one-time judge tip if due (see `references/judge-eval.md`) + judge block verbatim if the judge ran |
| After fix attempt | `[loop] session cycle N — fixes applied. Verifying via profile runner.` |
| After verify | `[loop] session cycle N — verified (<test summary>).` |
| Before push | `[loop] session cycle N — committing and pushing <commit-sha>...` |
| **Before push (HARD GATE)** | Run `--cycle-summary` — it writes the full receipt to the PR comment — and relay its `[loop] Cycle receipt` pointer line. Only then push. |
| After push | `[loop] session cycle N — pushed. Requesting reviewer re-review. Cap now M/K.` |
| After final re-review request | Wait after `REREVIEW_AT` before terminal recording; record `--gemini-confirmed` on success, `--gemini-unconfirmed` on timeout. |
| During any reviewer wait | Primary (background-capable runtime): run the wait as one background Bash task; relay its final status once on completion. Fallback: chunked waits (`--wait-chunk-seconds`), one-line heartbeat relayed per chunk. See Workflow step 11. |
| Stop condition | `[loop] STOP — <stop-condition>: <one-line explanation>.` |
| Loop complete (clean) | `[loop] DONE — 0 actionable threads remaining. Cycles used: N/<cap>.` |
| After DONE/STOP | Relay the `[loop] Summary` pointer line from `--record-run` (full receipt is in the PR comment). If `remaining_actionable > 0`, load `references/terminal-report.md` and render the three-bucket breakdown. |

Skip narration only in pure non-interactive batch mode. For visibility outside the chat (user stepping away), pair with `--sticky-receipt` (see `references/receipts-and-metrics.md`).

### Bundled hooks

Three hooks (`hooks/hooks.json`) make the most-skipped obligations mechanical. All are gated by local state (free no-ops outside an active loop) and fail open.

| Event | Script | Guarantees |
|---|---|---|
| `PreToolUse` (`Bash`) | `loop_summary_gate.py` | Blocks `git push` while a loop is active and the summary is stale; exit 2 names the exact `--cycle-summary` to run. |
| `PreToolUse` (`Edit`/`Write`/`MultiEdit`) | `loop_profile_gate.py` | Blocks edits while a loop is active and no verification profile is saved. Any saved profile — including `Skip` — clears it. |
| `Stop` | `loop_summary_hook.py` | If a loop advanced this turn without a summary, emits the authoritative `--cycle-summary`. Dedup-aware; read-only. |

## Optional Judge Eval

An opt-in, read-only OpenAI judge can classify each finding (`valid_actionable / false_positive / duplicate / already_addressed / explanation_only / needs_human`). **Off by default; nothing is sent to OpenAI unless the user opts in.** Phase is auto-inferred (`cycle` per fetch, `complete` at `--record-run`). When the saved mode is not `off`, the user mentions judge eval, or the one-time tip is due (first cycle with findings and `judge_tip_shown` ≠ true), load `references/judge-eval.md` for modes, prompts, key setup, and the prefs file.

## Stopping Conditions

Stop and report instead of pushing or re-asking when any is true:

1. **Cap reached** — re-review requests at the configured cap.
2. **All clean** — no `UNRESOLVED` actionable threads after cleanup.
3. **Human decision required** — all remaining threads are informational, duplicate, contradictory, or need a human product/design/security call. A thread deferred via substantive reply (`ADDRESSED_BY_REPLY`) is this condition, not condition 5.
4. **Test regression** — tests fail after a fix and the failure is not clearly caused by the finding-addressing change.
5. **No progress** — detected mechanically: when the actionable thread fingerprint is unchanged from the previous cycle, the script prints `[loop] no_progress: …`. Stop immediately: `--record-run --outcome no_progress --outcome-reason 'no code change resolved any open thread'`; do not push or re-request.

At the cap, still run cleanup, terminal classification, metrics recording, and the final summary. Re-invocation at the cap is usually a resume signal, not an instant stop — load `references/resume-and-recovery.md` before declaring a hard stop, and when new pushes land after the loop stopped.

## Workflow

1. **Trigger by default after PR creation.** "Create the PR", "ship this", "run the review loop" all authorize the full loop: wait, fetch, acknowledge, fix, verify, push, re-review.
2. **Resolve the PR.** Use the given URL/number, else `gh pr view --json number,url,headRefName,baseRefName` on the current branch. No PR → report the blocker.
3. **Select the reviewer** (see Reviewer Selection). Persisted → continue silently.
4. **Wait for the first review.** Check `reviewerSelection.auto_reviews` first: when `false` (Codex) and the PR has no review activity from that reviewer, post the trigger via `request_rereview.py` and wait with `--after`; waiting without pinging burns the whole timeout. Cycle 1 without `--after` returns as soon as activity is present (no settle). Run the wait per the step-11 wait protocol (background primary, chunked fallback). If the wait times out, say so — do not invent feedback. If the PR already has reviewer activity at session start, skip the wait and fetch (missed-trigger recovery; see `references/resume-and-recovery.md`).
5. **Check loop status.** Count prior agent-posted re-review requests; at or above the cap follow Stopping Conditions. Stale threads (outdated, addressed-by-reply) are auto-resolved unless opted out.
6. **Fetch reviewer threads** (persisted reviewer; default filter unresolved + not outdated).
7. **Acknowledge.** Summarize actionable findings grouped by file/behavior. None → report clean and stop. **Profile gate:** first run for the repo → make the profile decision NOW, before any edit (see Verification Profile). Then relay the profile intro block from the fetch output — except on that first run, where the fetch carries only the regenerate notice, so regenerate the intro from the just-saved profile with `--profile-intro --repo <owner/repo>` and relay that.
8. **Classify.** Actionable vs informational/duplicate/conflicting; explanation requests get a reply draft, not a forced edit; conflicts or regression risk → stop and surface the tradeoff.
9. **Implement fixes.** Scoped to feedback; read before editing; each change traceable to a feedback cluster. Sweep multi-site patterns per Pattern → Sweep → Converge.
10. **Verify.** Relay the planned-verification block (already in the fetch output; on a first run, regenerate it from the just-saved profile instead), run `run_profile.py`, feed results into `--verification`/`--verification-details`. No profile → narrowest meaningful checks. Checks can't run → report why.
11. **Commit, push, re-review, wait, record.**
    - Commit with a clear message (e.g. `fix: address AI reviewer findings`).
    - Non-terminal cycle: run `--cycle-summary` (delivers the full receipt to the PR comment), relay its `[loop]` pointer line, then push.
    - Post the re-review only if within the cap, via the helper (never hand-write the trigger):
      ```bash
      python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/request_rereview.py" \
        --repo OWNER/REPO --pr PR_NUMBER --json
      ```
      Capture `created_at` as `REREVIEW_AT`. `status: no_safe_trigger` → stop and relay the message exactly.
    - Wait for the reviewer. **Primary — runtimes with background-task completion notifications (Claude Code):** run the blocking wait as a **background** Bash task and continue only when the harness reports it finished, so a wait of any length costs one turn:
      ```bash
      python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
        --wait --after "$REREVIEW_AT" --timeout 1800
      ```
      The script writes a one-line liveness heartbeat to stderr every ~60s — the user can inspect the running task at any time. Do not poll the task in a loop, and never invent its result before the completion notification. When it exits, relay its final output once: success proceeds into the fetched threads; `refused` and `timed_out` end with a deterministic relayable block.
      **Fallback — runtimes without completion notifications (e.g. Codex):** chunked foreground waits:
      ```bash
      python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
        --wait --after "$REREVIEW_AT" --wait-chunk-seconds 60
      ```
      After each non-ready chunk relay the one-line heartbeat verbatim and start the next chunk with the script's `next_wait_seconds` — the script owns the 60s→300s decay; do not invent intervals.
      Statuses (both modes): `waiting`, `settling`, `ready` (same call returns the threads), `timed_out`, `refused`. On `refused`, stop waiting and relay the `[loop] STOP` block: `kind: withdrawn` → terminal, record `--outcome human --gemini-unconfirmed`; `kind: quota_exhausted` → ask the user now (Stop the loop, or upgrade/add credits then re-run the same wait) — do not re-request the review. On `timed_out`, record `--gemini-unconfirmed`; do not guess `clean`.
    - After the final wait, record exactly once with `--record-run` and relay its `[loop] Summary` pointer line (the full terminal receipt is in the PR comment).

## Script Usage

Resolve the runtime-neutral plugin root once, as its own Bash call:

```bash
GGRL_PLUGIN_ROOT="${GGRL_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$GGRL_PLUGIN_ROOT" ]; then
  GGRL_PLUGIN_ROOT=$(
    find ~/.codex/plugins ~/.codex/plugins/cache ~/.claude/plugins/cache \
      -type d -path "*/skills/gh-review-loop" 2>/dev/null \
      | sort -rV | head -1 | sed 's|/skills/gh-review-loop$||'
  )
fi
if [ -z "$GGRL_PLUGIN_ROOT" ] && [ -d "$(git rev-parse --show-toplevel 2>/dev/null)/plugins/gh-review-loop" ]; then
  GGRL_PLUGIN_ROOT="$(git rev-parse --show-toplevel)/plugins/gh-review-loop"
fi
export GGRL_PLUGIN_ROOT
```

Default fetch (resolves stale threads, prints current feedback):

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" [--pr <URL>]
```

**Delta mode.** Threads whose rendered block is unchanged since the previous cycle collapse to one line (anchor, severity, URL) — that is not missing data; the full body was already shown last cycle. Any change (new reply, edited body, moved anchor) renders full automatically. On a resumed session or after context compaction, run one fetch with `--full` to re-establish the baseline (see `references/resume-and-recovery.md`). The fetch output also ends with the profile intro + planned-verification blocks — relay them from there; no separate calls needed, except on a first run, where the fetch predates the profile decision and emits a regenerate notice instead.

The full option catalog (waits, discovery, read-only/dry-run, JSON, history) is in `references/script-usage.md`. The script warns on stderr when a GraphQL page limit is hit — older items may be missing.

## GitHub Write Safety

The default loop commits, pushes, requests re-review, and resolves stale threads. Policy:

- **OUTDATED** — auto-resolved. **ADDRESSED_BY_REPLY** — auto-resolved on the next pass (skip if the user said "don't resolve": `--no-resolve-addressed-by-reply`).
- **UNRESOLVED** — never resolved without an explicit user request. **Reviews (approve/request-changes)** — never submitted unless explicitly asked.
- Uncertain run → `--dry-run` first (logs intended resolutions to stderr, no GraphQL writes).

Stop before publishing if fixes are ambiguous, tests expose a regression, unrelated local changes make a clean commit unsafe, or the PR is at the cap.
