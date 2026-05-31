---
name: gh-gemini-review-loop
description: Use after a GitHub PR is opened, or when the user asks to handle gemini-code-assist review feedback, run the Gemini review loop, fix Gemini comments, or re-request Gemini review. Waits, fixes, pushes, re-asks. Capped by user preference, default 3 cycles.
---

# Gemini Code Assist PR Review Loop

## Overview

Use this skill to run the full GitHub PR loop: after PR creation, wait for `gemini-code-assist` to finish reviewing, fetch unresolved actionable review threads, acknowledge the requested fixes, implement clear fixes, verify them, commit and push to the PR branch, and ask Gemini Code Assist to re-review the latest revision.

Prefer thread-aware review data over flat PR comments. GitHub review threads preserve `isResolved`, `isOutdated`, file paths, line anchors, and diff hunks, which are necessary for reliable automation.

## Thread States

Each Gemini review thread is in one of these states. The fetch script tags each thread accordingly, and the stop logic consumes the tag:

- **`RESOLVED`** — Gemini or the maintainer has explicitly resolved the thread. Skip.
- **`OUTDATED`** — The line anchor has moved out from under the thread (the code Gemini commented on no longer exists at that position). Auto-resolved by the script. Skip.
- **`ADDRESSED_BY_REPLY`** — Unresolved, but the current user (or another maintainer) has posted a substantive reply (≥30 chars, not a bot, not a token "ack"). Treated as a human decision to defer/wontfix; the loop does not try to fix this thread again. The script auto-resolves these on the next pass via GraphQL (see "GitHub Write Safety" below). Opt out with `--no-resolve-addressed-by-reply`.
- **`UNRESOLVED`** — Actionable. Drives the next fix attempt.

A thread can transition `UNRESOLVED → ADDRESSED_BY_REPLY → RESOLVED` (reply + auto-resolve), or `UNRESOLVED → fixed in code → OUTDATED → RESOLVED` (line moves out, auto-resolved).

## Cycle Counting

A **cycle** is one `@gemini-code-assist please review` re-review request posted by the agent after Gemini's initial review.

- **Cycle 0:** Gemini's initial automatic review at PR open. Free; does not count toward the cap.
- **Cycles 1–N:** Each subsequent re-review request the agent posts, where `N` is `max_rereview_requests` from `~/.config/gh-gemini-review-loop/preferences.json` or the default `3`. After cycle `N`, hard stop.
- Replies posted via `repos/.../pulls/comments/{id}/replies` do **NOT** count as a cycle.
- Pushes to the PR branch without a re-review request do **NOT** count as a cycle.
- **Only re-reviews posted by the agent itself count.** A human pinging `@gemini-code-assist` does not consume a cycle. The script auto-detects the agent's GitHub login via `gh api user`; override with `--agent-login NAME` or opt out with `--no-agent-filter`.

## Severity Ordering

Gemini prefixes inline review comments with a markdown image whose alt text is the severity (`critical` / `high` / `medium` / `low`). The script parses this and orders actionable threads `critical → high → medium → low → unknown`, so high-severity findings are reported and fixed first. The severity tag also appears in the per-thread markdown header, e.g. `## 1. src/auth.py:42 [high]`.

## Loop Receipt

Pass `--post-receipt` to leave a one-comment audit trail on the PR after the loop runs: cycles used, threads resolved (outdated + addressed-by-reply), threads still pending, and severity breakdown of remaining actionable threads. Use `--dry-run --post-receipt` to preview the receipt without posting.

### Sticky receipt (background visibility)

For richer visibility while the loop is in flight, use `--sticky-receipt` instead. It maintains **one comment per PR that the script edits in place** across loop invocations. State persists in `~/.config/gh-gemini-review-loop/state.json` (override with `GGRL_STATE_DIR` env var).

- First invocation: posts a fresh comment with status `RUNNING`, stores its id locally.
- Subsequent invocations: PATCH the same comment in place. No new comments accrete on the PR.
- Status header is configurable via `--receipt-status {running,done,stopped}`. Default is `RUNNING` for sticky receipts.
- Tag the final invocation with `--receipt-status done` (or `stopped`) so the user sees the loop has finished.
- Discovery fallback: if the local state file is missing, the script searches PR comments for the embedded marker and re-attaches to the existing receipt.

When `--sticky-receipt` is appropriate: long-running interactive loops where the user is watching the PR tab, not the chat. Always paired with `--receipt-status running` at cycle start, `done` at clean exit, `stopped` at stop-condition.

When the one-shot `--post-receipt` is appropriate: scripted/batch contexts where each invocation is independent and you want a fresh audit comment per run.

## Optional Judge Eval (`--judge-mode`)

The Gemini review loop supports an optional OpenAI-based judge eval. It classifies each Gemini finding as one of `valid_actionable / false_positive / duplicate / already_addressed / explanation_only / needs_human`, plus a `severity_override` and `recommended_action`. The judge is **read-only** — it never resolves threads, posts comments, or pushes.

Judge eval is **off by default**. Nothing is sent to OpenAI unless the user explicitly opts in.

Requires `OPENAI_API_KEY` in env and the `openai` SDK installed. Missing either → judge gracefully skips with a structured `skipped` result + one stderr hint. The loop continues unchanged.

If the user asks how to set `OPENAI_API_KEY` permanently, recommend the macOS Keychain approach:
```bash
security add-generic-password -a "$USER" -s "openai-api-key" -w "sk-..."
echo 'export OPENAI_API_KEY=$(security find-generic-password -a "$USER" -s "openai-api-key" -w 2>/dev/null)' >> ~/.zshrc
```
This keeps the key out of plaintext dotfiles. On Linux, suggest `~/.config/environment.d/` or a secrets manager.

The script (`fetch_gemini_threads.py`) is the single source of truth. It reads `~/.config/gh-gemini-review-loop/preferences.json` on every invocation and combines the saved mode with the `--judge-phase` the agent supplies.

### Discoverability

Do **not** prompt for judge eval during a normal loop run. Do **not** prompt at session start.

**One-time tip — after fetch, before fixes.** On the first cycle where actionable findings are present, if `judge_tip_shown` is not `true` in the prefs file, emit this tip immediately after the findings narration line, then call `mark_tip_shown()` to persist `judge_tip_shown: true`:

```
[loop] cycle 1/<cap> — 4 actionable thread(s) (high: 1, medium: 3). Fixing.
[loop] Tip: judge eval can give a second opinion on these findings.
         Try: "run the Gemini loop with judge eval at completion"
```

The tip fires at the moment the user is looking at real findings — before any fixes are applied. It appears exactly once across all future sessions.

Use README examples, `--help` output, and marketplace description for broader discoverability.

### When to prompt

Prompt with `AskUserQuestion` **only** when the user explicitly requests judge eval without specifying a mode:

> **"enable judge eval" / "use judge eval" / "turn on eval"**

Prompt text:

> Judge eval sends Gemini findings and related PR context to OpenAI.
>
> Choose eval mode:
> 1. Every cycle
> 2. At completion only
> 3. Just this once
> 4. Off

Persist via `save_preferences()`. Mapping:
- 1 → `save_preferences("on_cycle")`
- 2 → `save_preferences("on_complete")`
- 3 → **do NOT save** — pass `--judge-mode once --judge-phase complete` for this run only
- 4 → `save_preferences("off")`

### Preference file

```
~/.config/gh-gemini-review-loop/preferences.json
```

```json
{
  "schema_version": 1,
  "judge_mode": "off",
  "judge_tip_shown": true,
  "max_rereview_requests": 3
}
```

Valid `judge_mode` values: `off`, `on_complete`, `on_cycle`. For one-time eval, do not modify the saved preference.

`max_rereview_requests` sets the persistent loop cap. The script reads it from `~/.config/gh-gemini-review-loop/preferences.json` on every invocation. The CLI flag `--max-rereview-requests N` overrides it for a single invocation.

To configure the persistent cap, create or edit:

```bash
mkdir -p ~/.config/gh-gemini-review-loop
python3 - <<'PY'
import json
from pathlib import Path

path = Path.home() / ".config" / "gh-gemini-review-loop" / "preferences.json"
prefs = json.loads(path.read_text()) if path.exists() else {}
prefs["schema_version"] = 1
prefs["max_rereview_requests"] = 4
path.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n")
PY
```

Or edit the JSON directly:

```json
{
  "schema_version": 1,
  "max_rereview_requests": 4
}
```

### Cost framing

`gpt-4o-mini` ≈ $0.001 per finding. `on_complete` ≈ $0.005 max per PR. `on_cycle` worst case depends on the configured cap (default: ≈ $0.015 for 3 cycles × 5 findings).

## Progress Narration

While the loop is running, the agent MUST emit one-line status updates to the user-facing chat at each phase transition. The format is `[loop] cycle N/<cap> — <phase>`. This is the cheapest user visibility — no code path, just instructions to the agent.

Required narration points:

| Phase | Narration line |
|---|---|
| Before script fetch | `[loop] cycle N/<cap> — fetching threads from PR #<num>...` |
| After fetch, before fixes | `[loop] cycle N/<cap> — <K> actionable thread(s) (severity: <breakdown>). Fixing.` + judge eval tip if first time (see [Discoverability](#discoverability)) |
| After fix attempt, before verify | `[loop] cycle N/<cap> — fixes applied. Verifying.` |
| After verify | `[loop] cycle N/<cap> — verified (<test summary>).` |
| Before push | `[loop] cycle N/<cap> — committing and pushing <commit-sha>...` |
| After push, before re-review | `[loop] cycle N/<cap> — pushed. Requesting Gemini re-review (cycle N consumed).` |
| Stop condition triggered | `[loop] STOP — <stop-condition>: <one-line explanation>.` |
| Loop complete (all clean) | `[loop] DONE — 0 actionable threads remaining. Cycles used: N/<cap>.` |

Skip narration only when running in pure non-interactive batch mode (e.g. `gh pr create` chained into a script that captures output for later — but in Claude Code interactive sessions, never skip).

Rationale: in interactive Claude Code sessions, the user is watching the chat. Silent loops feel broken even when they're working. One line per phase is the right cadence — enough to show progress without burying signal.

When the user explicitly wants visibility outside the chat (e.g. they'll step away from the terminal, or other reviewers will look at the PR while the loop runs), pair the chat narration with `--sticky-receipt`. See [Sticky receipt](#sticky-receipt-background-visibility) above.

## Variations (user-prompt → flag mapping)

When the user phrases the request differently, dispatch to the right flag combination. This table is authoritative; if a phrasing isn't here, fall back to defaults.

| User intent | Phrasing examples | Pass to script |
|---|---|---|
| **Default loop** | "run the gemini loop" / "handle gemini feedback" / "yeet this PR" | (no extra flags) |
| **High-severity only** | "only fix high severity" / "skip the nits" / "just the important stuff" | `--min-severity high` |
| **Medium and above** | "skip low-priority comments" | `--min-severity medium` |
| **Critical only** | "just the critical findings" | `--min-severity critical` |
| **Strict severity filter** | "only what Gemini flagged as high — ignore unmarked" | `--min-severity high --drop-unknown-severity` |
| **Audit-only** | "summarize Gemini comments" / "read-only review" / "show me what's pending" | `--dry-run --post-receipt --no-resolve-outdated --no-resolve-addressed-by-reply` |
| **More cycles once** | "be persistent" / "do 4 cycles" | `--max-rereview-requests 4` |
| **Fewer cycles once** | "one cycle only" / "don't loop, just fix once" | `--max-rereview-requests 1` |
| **Persistent cap** | "always use 4 cycles" / "configure the cap max to 4" | Set `max_rereview_requests` in `~/.config/gh-gemini-review-loop/preferences.json` |
| **Specific PR** | "handle PR https://github.com/..." | `--pr <URL>` |
| **Different bot login** | "handle review comments from google-gemini-code-assist" | `--author google-gemini-code-assist` |
| **Post status without acting** | "leave a status comment without touching anything" | `--post-receipt --no-resolve-outdated --no-resolve-addressed-by-reply` |
| **Live status comment** | "show me a live status comment on the PR" / "I want background visibility" | `--sticky-receipt --receipt-status running` per cycle; `--sticky-receipt --receipt-status done` at the final invocation |
| **Loop + judge at completion** | "run the Gemini loop with judge eval at completion" / "with judge eval at completion" | `save_preferences("on_complete")` + `--judge-phase complete` at final invocation. No prompt. |
| **Loop + judge every cycle** | "run the Gemini loop with judge eval on every cycle" / "with judge eval on every cycle" | `save_preferences("on_cycle")` + `--judge-phase cycle` each cycle. No prompt. |
| **Judge just this once** | "run judge eval just this once" / "with judge eval just this once" | `--judge-mode once --judge-phase complete`. No save. No prompt. |
| **Enable judge eval (no mode)** | "enable judge eval" / "use judge eval" / "turn on eval" | Show `AskUserQuestion` prompt; act on answer. |
| **Explain judge eval** | "what is judge eval?" / "how does judge eval work?" | Explain it. Do not enable it. |
| **Disable judge for this run** | "skip the judge this time" | `--judge-mode off` |
| **Change saved preference** | "change my eval preference" / "reset judge mode" | Show `AskUserQuestion` prompt; overwrite prefs file. |
| **Default loop with saved judge mode** | (no special phrasing — agent reads saved prefs) | `--judge-phase cycle` per cycle; `--judge-phase complete` at final invocation. Script obeys saved mode. |
| **History investigation** | "show me all Gemini threads ever, including resolved" | `--include-resolved --include-outdated --include-addressed-by-reply --no-resolve-outdated --no-resolve-addressed-by-reply` |

If the user explicitly opts out of any default behavior (e.g. "don't auto-resolve anything"), respect it for the rest of the session via `--no-resolve-outdated --no-resolve-addressed-by-reply`.

This skill does NOT support multi-bot loops (CodeRabbit, Copilot, etc.). It is opinionated for `gemini-code-assist` only. If the user asks for a different bot, change `--author`, but severity parsing and addressed-by-reply heuristics are calibrated for Gemini's output format.

## Stopping Conditions

Stop the loop and report status instead of pushing or asking Gemini again when any condition is true:

1. **Cap reached** — Gemini has already been asked to re-review the PR up to the configured cap.
2. **All clean** — There are no `UNRESOLVED` actionable Gemini threads after stale-thread cleanup.
3. **Human decision required** — All remaining `UNRESOLVED` threads are informational, duplicate, contradictory, or require a human product/design/security decision.
4. **Test regression** — Tests fail after a fix attempt and the failure is not clearly caused by the latest Gemini-addressing change.
5. **No progress** — A thread that was UNRESOLVED in the previous cycle is still UNRESOLVED after a fix attempt AND the surrounding code/hunk was not changed AND no substantive maintainer reply (as defined in Thread States) was posted on it. This catches genuine stuckness — distinct from ADDRESSED_BY_REPLY, which is intentional deferral and should not trip this condition.

If a thread was deliberately deferred via a substantive reply (state `ADDRESSED_BY_REPLY`), treat it as condition 3 (human decision), not condition 5 (no progress). The loop must not re-try the same fix on the same thread cycle after cycle.

Do not run more than the configured fix/re-review cap per PR. If the loop stops because the cap is reached, summarize the latest unresolved actionable comments and leave the PR for a human decision.

## Recovery: Missed Initial Trigger

The skill is meant to auto-trigger after `gh pr create`. If the agent forgets — e.g., the workflow that created the PR ended the turn at the PR URL without chaining into this skill — the loop must be invoked retroactively at the next opportunity:

- At session start (or whenever the skill is loaded), check if the current branch has an open PR.
- If yes, AND the latest commit has not been re-reviewed (no Gemini review activity on or after that commit's SHA), AND the agent has posted zero re-review trigger comments (e.g., "@gemini-code-assist please review"), run the loop now as catch-up cycle 0/1.
- This is a recovery clause only — it should not run silently on every session start in repos that don't use Gemini Code Assist. Skip if `gemini-code-assist` is not a configured reviewer on the repo.

## Follow-up Pushes After the Loop Stops

If the agent pushes new commits to a PR branch after the loop has already stopped:

- If any of those commits touch files where Gemini left `UNRESOLVED` or `ADDRESSED_BY_REPLY` threads, automatically resume the loop (subject to the configured cap).
- Otherwise stay stopped — Gemini's own automatic re-review on the new commit will run unattended, and the agent need not coordinate.

Doc-only commits (README, CLAUDE.md, comments) never resume the loop on their own.

## Workflow

1. Trigger the loop by default after PR creation.
   - When Claude creates or opens a PR and this skill is available, continue into this workflow automatically unless the user explicitly says not to.
   - Treat "create the PR", "open a PR", "yeet this", "ship this PR", and "run the Gemini loop" as permission to complete the full loop: wait, fetch, acknowledge, fix, verify, commit, push, and request Gemini re-review.

2. Resolve the PR.
   - If the user provides a PR URL, repo, or PR number, use it directly.
   - Otherwise, use the current git repository and branch:
     - `gh auth status`
     - `gh pr view --json number,url,headRefName,baseRefName`
   - If no PR exists for the branch, report that blocker.

3. Wait for Gemini to finish its first review.
   - Run `scripts/fetch_gemini_threads.py --wait` from this skill.
   - The script polls GitHub until Gemini review activity is present and stable for a quiet period.
   - By default, the script resolves unresolved outdated Gemini threads after the wait and before returning current feedback.
   - If the wait times out, report that Gemini did not finish within the timeout and do not invent feedback.

4. Check loop status and clean stale threads.
   - Count prior PR comments that ask `@gemini-code-assist` to review again.
   - If the count is already at or above the configured cap, stop before making changes, pushing, posting comments, or resolving threads.
   - If the count is below the configured cap, unresolved outdated Gemini review threads are resolved automatically; outdated threads are stale and should not drive new fixes.
   - For read-only inspection, pass `--no-resolve-outdated`.

5. Fetch Gemini review threads.
   - Default author filter: `gemini-code-assist`.
   - Default thread filter: unresolved and not outdated.
   - Use JSON output when another tool or script will consume the result; use Markdown for human triage.

6. Acknowledge what needs to be fixed.
   - Before editing, briefly summarize the actionable Gemini findings grouped by file or behavior.
   - If there are no actionable unresolved threads, say so and stop after reporting the clean result.

7. Classify comments.
   - Group by file and behavioral area.
   - Treat clear requested changes as actionable.
   - Ignore already resolved threads, outdated threads, approvals, duplicates, and informational comments.
   - If a thread asks for explanation rather than a code change, draft a response instead of forcing a code edit.
   - If comments conflict or could cause a behavioral regression, stop and surface the tradeoff.

8. Implement fixes.
   - Keep changes scoped to the Gemini feedback.
   - Read the relevant code before editing.
   - Preserve unrelated local changes.
   - Make each change traceable to a feedback cluster.

9. Verify.
   - Run the narrowest meaningful checks first.
   - Broaden tests when shared logic or user-facing behavior changes.
   - If checks cannot run, report why and what remains unverified.

10. Commit, push, and request re-review.
    - For this skill's full loop, commit fixes to the PR branch and push to the remote branch.
    - Use a clear commit message such as `fix: address Gemini Code Assist review`.
    - Post the re-review request after a successful push only if this would not exceed the configured total re-review request cap.
    - Default comment:
      - `@gemini-code-assist please review the latest changes.`
    - If the repository uses a different Gemini trigger phrase, use the repo-specific phrase when known.

## Script Usage

From any repository with a GitHub PR:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py"
```

By default this resolves unresolved outdated Gemini threads AND addressed-by-reply threads (unresolved threads where a non-bot maintainer posted a substantive reply, >=30 chars) before printing current feedback, unless the PR has already reached the configured re-review request cap.

Useful options:

```bash
# Wait for Gemini review activity to appear and settle
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --wait

# Read-only fetch (no GraphQL mutations)
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" \
    --no-resolve-outdated --no-resolve-addressed-by-reply

# Dry-run all resolutions (logs intended writes to stderr without calling GraphQL)
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --dry-run

# Specific PR URL
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --pr https://github.com/OWNER/REPO/pull/123

# JSON for automation
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --format json

# Include outdated, resolved, or addressed-by-reply threads while investigating history
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" \
    --no-resolve-outdated --include-outdated --include-resolved --include-addressed-by-reply

# Use a different bot login
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --author google-gemini-code-assist
```

The script emits `warning: ... hit page limit ...` to stderr if any GraphQL page maxes out (review threads, reviews, PR comments, or comments within a thread), indicating older items may be silently missing.

## GitHub Write Safety

This skill's default full loop includes committing, pushing, asking Gemini for re-review, and resolving outdated Gemini threads after PR creation or when the user asks for the Gemini loop.

**Resolution policy:**

- **OUTDATED threads** — auto-resolved (line anchor no longer matches code).
- **ADDRESSED_BY_REPLY threads** — auto-resolved on the next pass when a non-bot maintainer has posted a substantive reply (>=30 chars). Implemented in `scripts/fetch_gemini_threads.py` via `is_addressed_by_reply` and resolved through the same GraphQL mutation as outdated threads. This prevents the same thread from re-tripping the loop forever after a deliberate deferral. Skip if the user has said "don't resolve" earlier in the session (pass `--no-resolve-addressed-by-reply`).
- **UNRESOLVED threads** — never resolved without an explicit "resolve" request from the user.
- **Reviews (approve/request-changes)** — never submitted unless explicitly asked.

For any uncertain run, prefer `--dry-run` first: the script logs `[dry-run] would resolve <kind> <thread-id>` to stderr without calling GraphQL. Useful when debugging the reply-detection heuristic against a real PR.

Stop before publishing if the fixes are ambiguous, tests expose a regression, local unrelated changes make it unsafe to commit cleanly, or the PR has already reached the configured re-review request cap.
