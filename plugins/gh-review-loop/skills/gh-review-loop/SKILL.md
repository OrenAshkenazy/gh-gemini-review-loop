---
name: gh-review-loop
description: Use after a GitHub PR is opened, or when the user asks to handle AI reviewer feedback from Codex, CodeRabbit, Copilot, or another configured reviewer bot, run the review loop, fix reviewer comments, sweep sibling instances of a flagged pattern, or request re-review. Waits, fixes, verifies against the repo's own tests, pushes, re-asks. Capped by user preference, default 3 cycles.
---

# AI Reviewer PR Review Loop

## Overview

Use this skill to run the full GitHub PR loop: after PR creation, wait for the configured AI reviewer to finish reviewing, fetch unresolved actionable review threads, acknowledge the requested fixes, implement clear fixes, verify them, commit and push to the PR branch, and ask the reviewer to re-review the latest revision.

There is no default reviewer: with nothing configured the loop asks rather than assumes. On first use for a PR, discover reviewer candidates with `--list-reviewers`, confirm the chosen bot, and persist it with `--reviewer`. Gemini Code Assist is fully supported and behaves normally, including its automatic first review, for anyone on the enterprise GitHub app. Offer it like any other known vendor. The only caveat: its consumer app was shut down on 2026-07-17, so if a user picks it and no threads ever arrive, tell them that rather than waiting out the timeout. For compatible reviewer bots such as CodeRabbit, Copilot, Qodo, or Sourcery, pass a safe re-review mention only when it is known; never guess `@login` for an unknown bot.

Prefer thread-aware review data over flat PR comments. GitHub review threads preserve `isResolved`, `isOutdated`, file paths, line anchors, and diff hunks, which are necessary for reliable automation.

## Reviewer Selection

The loop supports one configured reviewer bot per PR. The first run with no
persisted reviewer must be prompt-first:

1. Run `fetch_gemini_threads.py --list-reviewers --format json`.
2. If candidates are returned, ask the user to confirm the single candidate or
   choose among multiple candidates, then persist the choice with
   `--reviewer <login> --reviewer-source confirmed [--reviewer-name <name>] [--review-trigger-mention <mention>]`.
3. If zero candidates are returned on a fresh PR, the selection comes back with
   `source: "none_configured"` and `configured: false`. Do not treat the
   accompanying `suggestion` as a choice already made. Report that no reviewer
   has commented and offer three things: **Ping the suggested reviewer**
   (`@codex review` — it reviews only on request, so accepting starts cycle 0
   immediately), **Pick another reviewer** (any known vendor, including Gemini
   Code Assist, or any bot whose login and re-review mention the user supplies),
   or **None**, which stops with "No AI reviewer threads found on this PR."
   Never wait on a reviewer the user did not choose. A reviewer that is not
   installed is indistinguishable from a slow one, and the wait burns the full
   timeout for nothing.
4. If the JSON result has `partial: true`, do not claim no reviewer exists; ask
   the user to choose manually or retry discovery.

Returning runs reuse the persisted reviewer silently. A new bot commenting later
does not switch the source automatically; use `--reviewer` to switch or
`--reset-reviewer` to rediscover.

### Known reviewers

Two reviewers ship with full vendor knowledge — display name, re-review
trigger, and whether they review a PR on their own:

| Reviewer | Login | Trigger posted | Priority format | Reviews unprompted |
|---|---|---|---|---|
| Gemini Code Assist | `gemini-code-assist` | `@gemini-code-assist please review the latest changes.` | `![high]` alt text | yes |
| Codex | `chatgpt-codex-connector` | exactly `@codex review` | `![P0]`–`![P3]` badges | **no** |

Codex priorities normalize to the shared scale (P0 → critical, P1 → high,
P2 → medium, P3 → low), so `--min-severity` works unchanged. Codex also puts
findings in the review body when no inline thread exists; the fetcher surfaces
those as current feedback and drops them once a newer Codex review supersedes
them.

Any other discovered bot still works, but the loop only re-asks it when a safe
mention is supplied with `--review-trigger-mention`.

When `fetch_gemini_threads.py --format json` reports
`reviewerSelection.confirmation_required: true`, stop before edits or
re-review requests and run the prompt flow above. `source:
default_unconfirmed` means the reviewer is only an unconfirmed default, not a
confirmed selection.

## Thread States

Each reviewer thread is in one of these states. The fetch script tags each thread accordingly, and the stop logic consumes the tag:

- **`RESOLVED`** — The reviewer or the maintainer has explicitly resolved the thread. Skip.
- **`OUTDATED`** — The line anchor has moved out from under the thread (the code the reviewer commented on no longer exists at that position). Auto-resolved by the script. Skip.
- **`ADDRESSED_BY_REPLY`** — Unresolved, but the current user (or another maintainer) has posted a substantive reply (≥30 chars, not a bot, not a token "ack"). Treated as a human decision to defer/wontfix; the loop does not try to fix this thread again. The script auto-resolves these on the next pass via GraphQL (see "GitHub Write Safety" below). Opt out with `--no-resolve-addressed-by-reply`.
- **`UNRESOLVED`** — Actionable. Drives the next fix attempt.

A thread can transition `UNRESOLVED → ADDRESSED_BY_REPLY → RESOLVED` (reply + auto-resolve), or `UNRESOLVED → fixed in code → OUTDATED → RESOLVED` (line moves out, auto-resolved).

## Cycle Counting

A **cycle** is one re-review request posted by the agent after the reviewer's first review.

- **Cycle 0:** The reviewer's first review — automatic for bots that self-review, or the agent's opening ping for bots that only review on request. Free; does not count toward the cap.
- **Cycles 1–N:** Each subsequent re-review request the agent posts, where `N` is `max_rereview_requests` from `~/.config/gh-gemini-review-loop/preferences.json` or the default `3`. After cycle `N`, hard stop.
- Replies posted via `repos/.../pulls/comments/{id}/replies` do **NOT** count as a cycle.
- Pushes to the PR branch without a re-review request do **NOT** count as a cycle.
- **Only re-reviews posted by the agent itself count.** A human pinging `@gemini-code-assist` does not consume a cycle. The script auto-detects the agent's GitHub login via `gh api user`; override with `--agent-login NAME` or opt out with `--no-agent-filter`.

The cap blocks **new re-review requests** and **new fix cycles** unless the user
raises the cap. The cap does **not** block stale-thread cleanup, addressed-by-
reply cleanup, metrics updates, terminal classification, final reports, or
recording terminal state. Even at the cap, the script may still resolve outdated
threads, classify fixed-pending findings, and print or record the final summary.

## Severity Ordering

Gemini prefixes inline review comments with a markdown image whose alt text is the severity (`critical` / `high` / `medium` / `low`); Codex uses `P0`–`P3`. These are the only two formats parsed today — every other bot's findings carry `unknown` severity. The script parses this and orders actionable threads `critical → high → medium → low → unknown`, so high-severity findings are reported and fixed first. The severity tag also appears in the per-thread markdown header, e.g. `## 1. src/auth.py:42 [high]`.

## Pattern → Sweep → Converge

Bot reviewers are LLMs: when one flags a code pattern, fixing only the flagged
sites teaches it to flag *more* instances of the same pattern in other changed
files next cycle. To collapse that expansion into one cycle, each cycle runs:

**Finding → Pattern → Sweep → Verify → Re-review**

1. **Cluster.** The cycle receipt's `Patterns (N):` section groups findings by a
   deterministic pattern signature. Reason about patterns, not a flat finding list.
2. **Sweep (report-then-go).** For each multi-site pattern (`count >= 2`), run
   `sweep_siblings.py` to find instances of the same shape the reviewer has not
   flagged yet, restricted to the PR's changed files:

   ```bash
   python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/sweep_siblings.py" \
     --signature <sig> --label "<label>" \
     --site <path:line> --site <path:line> \
     --changed-file <path> [--changed-file <path> ...] --json
   ```

   Pass every site from the cluster and every file in the PR's diff. The script
   intersects the tokens of the flagged lines and reports only lines containing
   all of them, so a candidate has to match what the flagged sites have in
   *common*. It reports; it never edits.

   Print the report, then fix the cluster plus the reported siblings in this
   cycle. Do not block on approval, but never edit unflagged code silently —
   the report must appear first.

   Honor the script's `status`. `too_few_sites`, `pattern_too_thin`, and
   `no_source` all mean **do not sweep**: fix the flagged sites only. When
   `truncated` is true the pattern is wider than a sweep should be — show the
   report and ask the user before touching unflagged code.
3. **Mark.** Pass `--swept-pattern <sig>` (the `sig:` token from the Patterns
   receipt) for each pattern you swept, alongside the usual `--fixed-finding`
   markers, so the convergence advisory can detect recurrence.
4. **Verify** (the repo profile) and **re-review** as usual.

The receipt's `Convergence:` line is advisory only. When a swept pattern reappears
("⚠ … RECURRED after sweep"), the sweep missed a variant or the reviewer keeps
re-flagging — decide whether to refine the sweep, stop, or continue. It never
changes control flow; the re-review cap remains the only hard stop.

Sweep scope is **changed files only** — that is both safe (blast radius = the PR's
own diff) and sufficient (bot reviewers only review changed files).

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

The review loop supports an optional OpenAI-based judge eval. It classifies each reviewer finding as one of `valid_actionable / false_positive / duplicate / already_addressed / explanation_only / needs_human`, plus a `severity_override` and `recommended_action`. The judge is **read-only** — it never resolves threads, posts comments, or pushes.

Judge eval is **off by default**. Nothing is sent to OpenAI unless the user explicitly opts in.

Requires an OpenAI API key resolved by `key_resolver.py` (env var → dotfile → macOS Keychain → Linux Secret Service). No SDK install needed — the judge uses stdlib `urllib`. Missing key → judge gracefully skips with a structured `skipped` result + one stderr hint. The loop continues unchanged.

If the user asks how to set their key permanently, recommend the OS-keystore path:
```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/key_resolver.py" --set
```
This stores in macOS Keychain (Touch ID / password-protected), Linux Secret Service, or a chmod-600 dotfile fallback. No rc-file edits, no `ps` leakage, no shell-restart dance. For diagnostics, suggest `key_resolver.py --print-source` and `judge_doctor.py`.

The script (`fetch_gemini_threads.py`) is the single source of truth. It reads `~/.config/gh-gemini-review-loop/preferences.json` on every invocation and combines the saved mode with the `--judge-phase` the agent supplies.

**Phase is auto-inferred.** You do not need to pass `--judge-phase`: a terminal
`--record-run` invocation is treated as phase `complete`; every other fetch is
phase `cycle`. So `on_cycle` runs on every cycle fetch and `on_complete` runs
only at the terminal record-run, with no per-cycle flag to remember. An explicit
`--judge-phase` still overrides the inference.

When `judge_mode=on_cycle`, show the deterministic judge block every cycle.
If the judge was requested but skipped, relay the script-owned line exactly:

```
[loop] judge eval skipped: <reason>
```

Do not hand-write a replacement judge table or skip explanation unless the
script fails.

### Discoverability

Do **not** prompt for judge eval during a normal loop run. Do **not** prompt at session start.

**One-time tip — after fetch, before fixes.** On the first cycle where actionable findings are present, if `judge_tip_shown` is not `true` in the prefs file, emit this tip immediately after the findings narration line, then call `mark_tip_shown()` to persist `judge_tip_shown: true`:

```
[loop] cycle 1/<cap> — 4 actionable thread(s) (high: 1, medium: 3). Fixing.
[loop] Tip: judge eval can give a second opinion on these findings.
         Try: "run the review loop with judge eval at completion"
```

The tip fires at the moment the user is looking at real findings — before any fixes are applied. It appears exactly once across all future sessions.

Use README examples, `--help` output, and marketplace description for broader discoverability.

### When to prompt

Prompt with the current runtime's choice-prompt mechanism **only** when the user explicitly requests judge eval without specifying a mode. In Claude Code this may be `AskUserQuestion`; in Codex use the available user-input flow or ask one concise question directly.

> **"enable judge eval" / "use judge eval" / "turn on eval"**

Prompt text:

> Judge eval sends reviewer findings and related PR context to OpenAI.
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
  "schema_version": 2,
  "judge_mode": "off",
  "judge_tip_shown": true,
  "max_rereview_requests": 3
}
```

The file is created automatically on the first script invocation with safe defaults (`judge_mode: off`, `max_rereview_requests: 3`). No manual setup is required.

**Key fields:**

- `judge_mode` — controls when the OpenAI judge eval runs. Valid values: `off`, `on_complete`, `on_cycle`. Set via natural language ("enable judge eval") or `save_preferences()`. Default: `off`.
- `max_rereview_requests` — persistent loop cap. Overridable per-run with `--max-rereview-requests N`. Default: `3`.
- `judge_tip_shown` — internal flag. `true` after the one-time "judge eval is available" tip has been shown in chat. Set automatically; do not edit manually.
- `judge_model` — OpenAI model used for eval. Default: `gpt-4o-mini`.

`max_rereview_requests` sets the persistent loop cap. The script reads it from `~/.config/gh-gemini-review-loop/preferences.json` on every invocation. The CLI flag `--max-rereview-requests N` overrides it for a single invocation.

To configure the persistent cap, create or edit:

```bash
mkdir -p ~/.config/gh-gemini-review-loop
python3 - <<'PY'
import json
from pathlib import Path

path = Path.home() / ".config" / "gh-review-loop" / "preferences.json"
prefs = json.loads(path.read_text()) if path.exists() else {}
prefs["schema_version"] = 2
prefs["max_rereview_requests"] = 4
path.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n")
PY
```

Or edit the JSON directly:

```json
{
  "schema_version": 2,
  "max_rereview_requests": 4
}
```

### Cost framing

`gpt-4o-mini` ≈ $0.001 per finding. `on_complete` ≈ $0.005 max per PR. `on_cycle` worst case depends on the configured cap (default: ≈ $0.015 for 3 cycles × 5 findings).

## Verification Profile

Each repo can have an opinionated, code-derived **verification profile**: the
checks the loop runs at its verify step. Stored in
`~/.config/gh-gemini-review-loop/preferences.json` under `profiles["owner/repo"]`.

### First run (detect → preset menu → save)

Run detection **after fetching findings, before the first fix attempt**, so the
verification strategy is fixed before any edits. This ordering is enforced by a
`PreToolUse` hook that blocks edits during an active loop until a profile is
saved (see [Bundled hooks](#bundled-hooks)). If `get_profile(owner/repo)`
returns `None`:

Detection precedence (monorepo-aware): if the repo root has a `justfile` with
verification recipes (`test`, `test-*`, `*-tests`, `check`, `lint`, `typecheck`,
`verify`) that take no required arguments, those become `just <recipe>` checks.
Otherwise, test directories discovered in the git tree (`tests`, `__tests__`,
`spec`, …) are each mapped to their nearest package marker and emitted with a
per-check `working_directory`. Otherwise, root single-stack detection applies.
A check may carry its own `working_directory` (relative to the repo root);
`run_profile` runs it there, falling back to the profile-level
`working_directory`.

1. Run `detect_profile.py <repo_root>`. It returns `{stack, confidence, reasons,
   candidate_checks, presets}`. `presets` is an explicit, ordered, code-built
   option list — do **not** hand-roll the menu or rely on the runtime prompt UI
   auto-adding an option.
2. If `stack == "unknown"` (empty `candidate_checks`, empty `presets`) → do
   **not** prompt or persist; use ad-hoc verification.
3. Reconcile against repo docs (`CLAUDE.md`, `CONTRIBUTING`, `README`). If docs
   pin a non-standard invocation, surface it as a note beside the menu — *"Repo
   docs pin `/opt/homebrew/bin/pytest`; pick **Customize manually** to use it."*
   Never auto-persist an absolute path from prose.
4. Prompt once with the current runtime's choice-prompt mechanism, using each
   `presets[i].label` verbatim as an option. Example menu for a multi-check Python repo:
   *"All detected — pytest + ruff check ." / "Tests only — pytest" / "Skip — use
   ad-hoc verification" / "Customize manually"*.
5. Persist the chosen preset via `judge.save_profile(...)`:
   - Has `customize == true` (**Customize manually**) → run the free-form NL
     customize path; persist the user's edited checks with `source="customized"`.
   - Otherwise persist `preset["checks"]` with `source=preset["source"]`
     (`confirmed` for All detected, `customized` for a narrower preset, `skipped`
     for Skip). Every persisted check is `required: true`.

### Subsequent runs

A profile (including a `skipped` one) exists → **no prompt**. If `source` is
`confirmed` or `customized`, first render and relay the deterministic profile
intro block:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
  --profile-intro \
  --repo OWNER/REPO
```

Then run `run_profile.py <owner/repo> <repo_root>` — it prints `to_details()`
JSON and exits non-zero if a required check failed; feed its `verification` into
`--verification` and the JSON into `--verification-details`. On `skipped` or
unknown stack, relay the formatter's fallback profile-intro block and use
today's ad-hoc "narrowest meaningful checks".

### Customizing / un-skipping

`source="skipped"` suppresses **automatic** detection prompts only. Explicit user
intent always overrides it:

- *"add mypy to this repo's verification profile"* / *"change the checks to X"* →
  edit the profile via `save_profile(..., source="customized")`.
- *"set up a verification profile for this repo"* → re-enter the detect → preset
  menu → save flow and overwrite the profile, **even if** a `skipped` marker
  exists.

### Gate semantics

The verify step **fails iff any `required` check fails or times out**.
Non-required failures are recorded in `--verification-details` but do not flip
`--verification` to `failed`. Feed `ProfileRunResult.to_details()` into
`--verification-details` and its `verification` field into `--verification`.

Before running checks, render and relay the deterministic planned-verification
block:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
  --planned-verification \
  --repo OWNER/REPO
```

Then run the profile runner. If no runnable profile exists, the formatter says
so; continue with ad-hoc verification.

## Run Metrics

After a loop completes, the script can append one JSON record to a local append-only log and print a one-screen summary.

### What is stored and where

Records are appended to `~/.config/gh-gemini-review-loop/runs.jsonl` (override the directory with `GGRL_STATE_DIR`). The file is append-only JSONL — one record per completed loop run. It is never transmitted anywhere.

Each record holds counts only: findings fetched, fixed, needs-human, addressed-by-reply, cycles used, verification result, outcome, duration (seconds), finding areas/paths, and optionally a judge-derived breakdown (only when judge mode was on). The record also includes the repo and PR number so stats can be scoped per repo.

**No identity is recorded** — no git author, no GitHub login, no username. The data cannot be sliced per developer and cannot become a productivity score.

The run's start timestamp and per-finding accumulation reuse the same per-PR key in `state.json` (see [Sticky receipt](#sticky-receipt-background-visibility)) — no extra state file is needed. Judge-derived lines (e.g. false-positive counts) appear in the record and summary only when judge mode was on for that run.

### `--record-run` (write once at loop end)

Call exactly once after the loop reaches a terminal state:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --record-run \
    --fixed-count <n> \
    --verification <passed|failed|skipped>
```

Optional additions:

- `--verification-details '<json>'` — structured test output
- `--outcome <clean|capped|human|regression|no_progress|verification_failed|fixed_pending_confirmation>` — terminal state label
- `--outcome-reason '<text>'` — one-line explanation
- `--gemini-confirmed` — final wait/re-review completed and confirmed the latest state
- `--gemini-unconfirmed` — final wait timed out or otherwise did not confirm the latest fixes
- `--fixed-finding <fp>` — finding fingerprint the agent fixed locally; repeatable; stored in run tracking and used for terminal classification
- `--swept-pattern <sig>` — pattern-level swept marker; the `sig:` token from the `Patterns (N):` receipt; repeatable; feeds the convergence advisory to detect recurrence after a sweep (see [Pattern → Sweep → Converge](#pattern--sweep--converge))

The script fetches the current thread state, derives counts, appends the record, and prints a `[loop] Summary` block to stdout.

### Per-cycle summary block

`--record-run` is terminal: it writes a record **and clears** the run accumulator, so it must be called exactly once at loop end. To show a mid-loop receipt **during** the loop — at the end of each cycle — use `--cycle-summary` instead:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --cycle-summary \
    --fixed-count <n-this-cycle> \
    --verification <passed|failed|skipped>
```

`--cycle-summary` builds the record from the **accumulated** run state (findings and judge verdicts unioned across cycles so far) and prints a `[loop] Cycle receipt` block — distinct from the terminal `[loop] Summary` — but it does **not** append to `runs.jsonl` and does **not** clear the accumulator. It is read-only and safe to call every cycle.

The receipt block also carries two sections that used to be buried in collapsed
tool output. Relay the **entire** receipt verbatim so they reach the chat:

- **`Verification suite:`** — the repo-aware checks `run_profile.py` detected
  (e.g. `uv run pytest  (root, required) → passed`). Built from
  `--verification-details`; present whenever you pass profile JSON.
- **`Findings (N): X new, Y carried over`** — the deterministic finding list,
  one line per finding with `path:line [severity]` and the GitHub comment URL.
  A finding whose content was already seen in a prior cycle is tagged
  `· carried over from a prior cycle`, so a re-posted suggestion is never
  miscounted as a fresh finding. This is the canonical, visible finding list —
  do not hand-roll a separate one or leave it inside a collapsed Bash result.

**Emit a receipt at the end of every cycle.** Which command depends on whether the cycle is also terminal:

- **Non-terminal cycle** (the loop will push, re-review, and continue): run `--cycle-summary` right after the verify step. This is REQUIRED on every such cycle — do not skip it because fixes were small or verification was skipped.
- **Terminal cycle** (this cycle hits a [stopping condition](#stopping-conditions) — clean, capped, human decision, regression, or no-progress): do **not** call `--cycle-summary`. The single `--record-run` call you make at loop end already prints the receipt for this cycle. Calling both would print two near-identical receipts back-to-back.

So a clean two-cycle run prints two receipts: one `[loop] Cycle receipt` from `--cycle-summary` at the end of cycle 1, one `[loop] Summary` from `--record-run` at loop end. A run that stops on cycle 1 (like a human-decision deferral) prints exactly one receipt — `[loop] Summary` from `--record-run`. Never emit two receipts on the same cycle.

Do not call `--record-run` more than once per loop: each call clears the accumulator, so a second call would undercount `findings_fetched` and reset the duration. Use `--cycle-summary` for all mid-loop visibility.

### Script-owned human blocks

For human-readable loop narration, prefer deterministic script output and relay
it verbatim. Do not hand-write or paraphrase these blocks unless the script
fails:

- profile intro (`--profile-intro`)
- planned verification (`--planned-verification`)
- judge eval table or judge skip line
- cycle receipt (`--cycle-summary`)
- terminal summary (`--record-run`)
- semantic-risk note (`--semantic-risk`, repeatable)
- next options for non-clean terminal outcomes
- wait heartbeat (`--wait-chunk-seconds` pending output, or `--wait-heartbeat`)

Before push or before the reviewer confirms the re-review, use the script's fixed-
pending wording. Locally fixed work must appear as `Fixed locally` plus
`Awaiting push/re-review confirmation`, not as `Remaining valid actionable`.

For non-clean terminal summaries, rely on the deterministic `Next options:`
section emitted by the formatter. Do not replace it with free-form prose.

### Semantic risk note

`--semantic-risk` is a manual / heuristic v1 signal. Pass it when a fix changes
behavior in ways that passing tests may not fully cover, especially:

- public function signature changes
- return-shape changes
- password/auth/security behavior
- database query behavior
- exception behavior
- public API behavior

Example:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --cycle-summary \
    --fixed-count <n-this-cycle> \
    --verification passed \
    --semantic-risk "hash_password(password) -> hash_password(password, salt)" \
    --semantic-risk "get_user() now returns one row instead of a list"
```

Relay the resulting `[loop] Semantic risk note (manual / heuristic)` block
verbatim. Do not present it as deterministic detection.

### Color and JSON output

Human-readable `[loop]` blocks are purple/magenta by default. Do not strip ANSI
color from normal human-readable loop output; the `[loop]` prefix remains
present for searchability and accessibility.

Color must never appear in JSON, GitHub comments, files, metrics, receipts
stored on disk, or any machine-readable output. For every `--json` or
`--format json` command, stdout is machine JSON only: parse stdout with
`json.loads(...)`, treat logs and warnings as stderr, and do not relay raw JSON
unless the user explicitly asked to see machine output.

### `--stats` (read-only)

Print aggregated stats for the current repo from `runs.jsonl` and exit. Never touches GitHub.

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --stats
```

Options: `--stats-window N` (default 10 most-recent runs), `--stats-all-repos`, `--format json`.

Run metrics and `--stats` are local-only — stored under `~/.config/gh-gemini-review-loop/`, never posted to GitHub, and contain no identity.

## Progress Narration

<HARD-GATE>
DO NOT run `git push` or call `--record-run` until you have:
1. Called `--cycle-summary` (for a non-terminal cycle) OR confirmed `--record-run` is the terminal call.
2. For a terminal cycle after a final push, requested reviewer re-review,
   captured `REREVIEW_AT`, waited with `--wait --after "$REREVIEW_AT"`, and
   set the terminal record's reviewer confirmation flag from that wait result.
3. Printed the FULL stdout of that script call verbatim in your text response to the user.

This is enforced mechanically: a PreToolUse:Bash hook (`loop_summary_gate.py`) blocks every `git push` while a review loop is active and the summary is stale. The hook does NOT fire for `--record-run` (the terminal receipt is exempt). Trying to push without summarizing first returns exit code 2 and explains the fix.

Violating the letter of this rule violates the spirit.
</HARD-GATE>

While the loop is running, the agent MUST emit one-line status updates to the user-facing chat at each phase transition. The format is `[loop] cycle N/<cap> — <phase>`.

**Mechanical backstop layer (three independent enforcers):**

1. **`loop_summary_gate.py` (PreToolUse:Bash)** — Blocks `git push` when `update_seq > last_summary_seq`. Exit code 2 with an error message tells the agent exactly which `--cycle-summary` command to run first. This is the primary gate: the agent cannot push a new cycle's commits without first emitting the summary.

2. **`loop_summary_hook.py` (Stop)** — On every turn end, if a loop advanced without a summary being emitted, runs `--cycle-summary --auto-snapshot` and surfaces the result through the runtime hook output (`systemMessage` in Claude Code). Catches the terminal-cycle gap (between `--record-run` output existing in Bash tool output and the agent relaying it to the user).

3. **Memory feedback** — Saved in the session memory to reinforce the rule across future sessions.

Required narration points:

| Phase | Narration line |
|---|---|
| Before script fetch | `[loop] session cycle N — re-review cap: M/K consumed. Fetching threads from PR #<num>...` where N is the cycle number within this session (1-based), M is how many re-reviews already exist on the PR, K is the cap. This is the key fix for the "cycle 1/4 but cap is 3/4" confusion: always show both session-local position and cap state separately. |
| After fetch, before fixes | `[loop] session cycle N — <K> actionable thread(s) (severity: <breakdown>). Fixing.` + judge eval tip if first time (see [Discoverability](#discoverability)) + if judge ran: copy the entire `[loop] judge eval (phase): …` block from script stdout verbatim (header + one row per thread) |
| After fix attempt, before verify | `[loop] session cycle N — fixes applied. Verifying via profile runner.` |
| After verify | `[loop] session cycle N — verified (<test summary>).` |
| Before push | `[loop] session cycle N — committing and pushing <commit-sha>...` |
| **Before push (HARD GATE)** | Run `--cycle-summary` and print its full output (`[loop] Cycle receipt` block). Only then push. |
| After push, before re-review | `[loop] session cycle N — pushed. Requesting reviewer re-review. Cap now M/K.` |
| After final re-review request | Wait for the configured reviewer after `REREVIEW_AT` before terminal recording. If wait succeeds, record with `--gemini-confirmed`; if it times out, record with `--gemini-unconfirmed`. |
| During any reviewer wait | Run chunked waits (`--wait-chunk-seconds`); after each non-ready chunk relay the script's heartbeat block verbatim, then start the next chunk. Never background the wait; never go silent for more than ~90s. |
| Stop condition triggered | `[loop] STOP — <stop-condition>: <one-line explanation>.` |
| Loop complete (all clean) | `[loop] DONE — 0 actionable threads remaining. Cycles used: N/<cap>.` |
| Loop complete / stopped (after DONE/STOP) | `[loop] Summary` block (from `--record-run`) — print the full stdout; this is the terminal receipt |

### Terminal thread breakdown (when remaining_actionable > 0)

After printing the `[loop] Summary` block, when there are remaining actionable threads, render them in **three separate buckets** — never a single mixed "for human review" table. The buckets map directly to why each thread is still open:

**Always reference a thread by its GitHub comment URL** (e.g.
`https://github.com/<owner>/<repo>/pull/<n>#discussion_r3374837147`), which the
receipt's `Findings` block already prints per finding. Never surface the bare
`discussion_r…` / GraphQL node token on its own — it is opaque to the user and
not clickable. If you need to point at a thread to resolve, give the URL and,
when useful, the `file:line`.

**Bucket 1 — Human decision required**

Threads where the judge verdict was `needs_human`. These require a product/format/design call, not a code change. For each thread:

```
Human decision required

1. <file>:<line> · <GitHub comment URL>
   Finding: <what the reviewer flagged, verbatim or closely paraphrased>
   Why human: <concrete reason — format consistency, security policy, product behavior tradeoff>
   The agent did not auto-fix this because <specific reason: changes report format behavior / requires policy decision / both options are valid>.
   Options:
   - <option A>
   - <option B>
```

Example:
```
Human decision required

1. main.py:828 · https://github.com/Owner/Repo/pull/9#discussion_r3369882171
   Finding: OWASP tags appear in console and JSON reports, but not in Markdown.
   Why human: this changes report format behavior. Both choices are valid.
   The agent did not auto-fix this because it is a product decision, not a safe mechanical fix.
   Options:
   - Add OWASP tags to Markdown output for consistency.
   - Keep Markdown simpler; document that OWASP tags are JSON/console only.
```

**Bucket 2 — Remaining because cap was reached**

Threads that the judge classified as `valid_actionable` but the loop ran out of cycles before addressing them. **Do not label these as "human review"** and do not downgrade their severity — they remain valid, fixable issues. Do not use "low priority" unless the judge, reviewer, or user explicitly said so.

```
Remaining because cap was reached

1. <file>:<line> · <GitHub comment URL>
   Finding: <one-sentence description>
   Judge: valid_actionable (conf <N>)
   Reason not fixed: cap reached
   Suggested handling: fix in next PR, or bump cap and re-run the loop

2. ...
```

**Bucket 3 — Already fixed but still unresolved on GitHub**

Threads where you applied a code fix in this loop, but the GitHub thread still shows UNRESOLVED. These are not open work items — they will become OUTDATED on the next reviewer pass.

```
Already fixed but still unresolved on GitHub

1. <file>:<line> · <GitHub comment URL>
   Finding: <what the reviewer originally flagged>
   Status: fix applied in this session
   Why still shown: code change shifts the line anchor; thread auto-resolves as OUTDATED on the next reviewer pass
```

**Omit any bucket with zero entries.** If every remaining thread fits bucket 1, just print bucket 1. Never print an empty bucket header.

**Classification logic (in order of priority):**

1. Judge verdict `needs_human` → bucket 1
2. Thread from a prior reviewer pass where you applied a code fix at that file/line in this session → bucket 3
3. All other `valid_actionable` threads (new from latest review, or no fix attempted) → bucket 2

**After the buckets — next step suggestions:**

Use the deterministic `Next options:` section emitted by the terminal summary
formatter. Do not hand-write the options unless the script fails.

**Verification routing:**

Always run verification through `run_profile.py` when a profile is confirmed for the repo:
```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
  --planned-verification \
  --repo owner/repo
```

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/run_profile.py" owner/repo /path/to/repo
```

Relay the planned-verification block before running checks. Feed the runner's
`verification` field into `--verification` and its full JSON output into
`--verification-details`. Do not call `uv run pytest` or any test runner
directly — route through the profile so metrics get consistent structured
details. This matters even when you know the profile command (the runner times
it, captures structured output, and sets the right exit code for downstream
processing).

Skip narration only when running in pure non-interactive batch mode (e.g. `gh pr create` chained into a script that captures output for later — but in interactive Claude Code or Codex sessions, never skip).

Rationale: in interactive Claude Code sessions, the user is watching the chat. Silent loops feel broken even when they're working. One line per phase is the right cadence — enough to show progress without burying signal.

When the user explicitly wants visibility outside the chat (e.g. they'll step away from the terminal, or other reviewers will look at the PR while the loop runs), pair the chat narration with `--sticky-receipt`. See [Sticky receipt](#sticky-receipt-background-visibility) above.

## Bundled hooks

The plugin ships three hooks (`hooks/hooks.json`) that turn the most-skipped
agent obligations into mechanical guarantees. All are network-gated by local
`state.json` (free no-ops outside an active loop) and all fail open: a hook
error never wedges the session.

| Event | Script | What it guarantees |
|---|---|---|
| `PreToolUse` (`Bash`) | `loop_summary_gate.py` | Blocks `git push` while a loop is active and `summary_is_stale()`. Exit code 2 with the exact `--cycle-summary` command to run. Primary gate — the agent physically cannot push without summarizing first. |
| `PreToolUse` (`Edit`/`Write`/`MultiEdit`) | `loop_profile_gate.py` | Blocks edits while a loop is active for the repo and no verification profile is saved, so the verify strategy is fixed before any fix. Saving any profile — including `Skip` — clears the gate. |
| `Stop` | `loop_summary_hook.py` | If a loop advanced this turn without a `[loop] Summary`, emits the authoritative `--cycle-summary`. Dedup-aware via `update_seq`/`last_summary_seq` — silent when the agent already summarized. Read-only; never records or clears. |

These mean the agent should still narrate and summarize as documented, but a lapse no longer costs the user visibility or breaks the profile-before-fixes ordering.

## Variations (user-prompt → flag mapping)

When the user phrases the request differently, dispatch to the right flag combination. This table is authoritative; if a phrasing isn't here, fall back to defaults.

| User intent | Phrasing examples | Pass to script |
|---|---|---|
| **Default loop** | "run the AI reviewer loop" / "handle reviewer feedback" / "run the gemini loop" / "yeet this PR" | (no extra flags) |
| **High-severity only** | "only fix high severity" / "skip the nits" / "just the important stuff" | `--min-severity high` |
| **Medium and above** | "skip low-priority comments" | `--min-severity medium` |
| **Critical only** | "just the critical findings" | `--min-severity critical` |
| **Strict severity filter** | "only what the reviewer flagged as high — ignore unmarked" | `--min-severity high --drop-unknown-severity` |
| **Audit-only** | "summarize reviewer comments" / "read-only review" / "show me what's pending" | `--dry-run --post-receipt --no-resolve-outdated --no-resolve-addressed-by-reply` |
| **More cycles once** | "be persistent" / "do 4 cycles" | `--max-rereview-requests 4` |
| **Fewer cycles once** | "one cycle only" / "don't loop, just fix once" | `--max-rereview-requests 1` |
| **Persistent cap** | "always use 4 cycles" / "configure the cap max to 4" | Set `max_rereview_requests` in `~/.config/gh-gemini-review-loop/preferences.json` |
| **Specific PR** | "handle PR https://github.com/..." | `--pr <URL>` |
| **Select Codex** | "run the Codex loop" / "use Codex for this PR" | `--reviewer chatgpt-codex-connector --reviewer-source confirmed` (name and trigger are known) |
| **Different reviewer bot** | "handle review comments from coderabbitai" | `--reviewer coderabbitai --review-trigger-mention @coderabbitai --reviewer-name CodeRabbit` |
| **Post status without acting** | "leave a status comment without touching anything" | `--post-receipt --no-resolve-outdated --no-resolve-addressed-by-reply` |
| **Live status comment** | "show me a live status comment on the PR" / "I want background visibility" | `--sticky-receipt --receipt-status running` per cycle; `--sticky-receipt --receipt-status done` at the final invocation |
| **Loop + judge at completion** | "run the AI reviewer loop with judge eval at completion" / "with judge eval at completion" | `save_preferences("on_complete")`. Phase is auto-inferred (`complete` at `--record-run`). No prompt. |
| **Loop + judge every cycle** | "run the AI reviewer loop with judge eval on every cycle" / "with judge eval on every cycle" | `save_preferences("on_cycle")`. Phase is auto-inferred (`cycle` per fetch, `complete` at `--record-run`). No prompt. |
| **Judge just this once** | "run judge eval just this once" / "with judge eval just this once" | `--judge-mode once --judge-phase complete`. No save. No prompt. |
| **Enable judge eval (no mode)** | "enable judge eval" / "use judge eval" / "turn on eval" | Show the runtime choice prompt; act on answer. |
| **Explain judge eval** | "what is judge eval?" / "how does judge eval work?" | Explain it. Do not enable it. |
| **Disable judge for this run** | "skip the judge this time" | `--judge-mode off` |
| **Change saved preference** | "change my eval preference" / "reset judge mode" | Show the runtime choice prompt; overwrite prefs file. |
| **Default loop with saved judge mode** | (no special phrasing — agent reads saved prefs) | No `--judge-phase` needed — phase is auto-inferred. Script obeys saved mode. |
| **History investigation** | "show me all reviewer threads ever, including resolved" | `--include-resolved --include-outdated --include-addressed-by-reply --no-resolve-outdated --no-resolve-addressed-by-reply` |
| **Local stats** | "show reviewer loop stats" / "loop stats for this repo" / "how's the loop doing here" | `--stats` |
| **Set up verification profile** | "set up a verification profile for this repo" / "configure checks for this repo" | Run `detect_profile.py`, show preset menu, then persist the chosen preset |
| **Customize profile** | "add mypy to this repo's checks" / "change the verification checks to X" | Edit checks, `save_profile(..., source="customized")` |
| **Skip profile** | "skip verification profile" / "use ad-hoc checks for this repo" | `save_profile(repo, source="skipped")` — suppress automatic re-prompt |

Run metrics and `--stats` are local-only — stored under `~/.config/gh-gemini-review-loop/`, never posted to GitHub, and contain no identity.

If the user explicitly opts out of any default behavior (e.g. "don't auto-resolve anything"), respect it for the rest of the session via `--no-resolve-outdated --no-resolve-addressed-by-reply`.

This skill supports one configured reviewer bot per run. It does not aggregate several reviewers into one combined loop. Codex is the default adapter; for another compatible bot, pass `--reviewer`, `--review-trigger-mention`, and `--reviewer-name`. Severity parsing reads markdown image alt text — both the shared scale (`![high]`, `![medium]`, …) and Codex's `![P0]`–`![P3]` badges — so reviewers without either marker fall back to `unknown` severity.

## Stopping Conditions

Stop the loop and report status instead of pushing or asking the reviewer again when any condition is true:

1. **Cap reached** — the reviewer has already been asked to re-review the PR up to the configured cap.
2. **All clean** — There are no `UNRESOLVED` actionable reviewer threads after stale-thread cleanup.
3. **Human decision required** — All remaining `UNRESOLVED` threads are informational, duplicate, contradictory, or require a human product/design/security decision.
4. **Test regression** — Tests fail after a fix attempt and the failure is not clearly caused by the latest finding-addressing change.
5. **No progress** — A thread that was UNRESOLVED in the previous cycle is still UNRESOLVED after a fix attempt AND the surrounding code/hunk was not changed AND no substantive maintainer reply (as defined in Thread States) was posted on it. This catches genuine stuckness — distinct from ADDRESSED_BY_REPLY, which is intentional deferral and should not trip this condition.

   The script detects this mechanically: when the actionable thread fingerprint (SHA256 of thread ids + bodies) is identical to the previous cycle's, it prints:
   ```
   [loop] no_progress: actionable thread set is unchanged since the previous cycle — no fix landed. Stop with: --record-run --outcome no_progress ...
   ```
   When this line appears in script stdout, **stop immediately**: call `--record-run --outcome no_progress --outcome-reason 'no code change resolved any open thread'` and do not push or request another review.

If a thread was deliberately deferred via a substantive reply (state `ADDRESSED_BY_REPLY`), treat it as condition 3 (human decision), not condition 5 (no progress). The loop must not re-try the same fix on the same thread cycle after cycle.

Do not run more than the configured fix/re-review cap per PR. If the loop stops
because the cap is reached, do not request another re-review or start another
fix cycle unless the user raises the cap. Still run stale-thread cleanup,
terminal classification, metrics recording, and the final script-generated
summary.

## Resuming After the Cap

Re-invoking the loop on a PR already at the cap is usually a **resume signal**,
not an instant stop. Evaluate these cases as a **strict priority order — first
match wins, top to bottom**:

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 (highest) | User increased the cap (effective `max_rereview_requests` > the cap already consumed) | **Continue** from the next cycle |
| 2 | Interrupted local work **not pushed** (local commits/edits beyond the remote branch HEAD) | **Finish the push** — no new cycle consumed |
| 3 | **Pushed** but no re-review request posted for that pushed SHA | **Request review** for that SHA — no new cycle consumed |
| 4 (lowest) | No new local work **and** no higher cap | **Hard stop** (Stopping Condition 1) |

Stop at the first matching priority. A bumped cap (priority 1) wins even if
unpushed work also exists. Cases 2 and 3 complete an already-started cycle and
do **not** consume a new cycle.

> **Follow-up (deterministic detection).** Cases 2 and 3 are prose-detected for
> now and MUST become deterministic so "resume" does not drift between sessions:
> case 2 needs a local-vs-remote SHA comparison (`git rev-parse HEAD` vs
> `@{u}`); case 3 needs a GitHub check that the latest pushed SHA has no
> following agent-posted re-review comment. Tracked in
> `docs/superpowers/specs/2026-06-06-pr37-followup-design.md`.

## Recovery: Missed Initial Trigger

The skill is meant to auto-trigger after `gh pr create`. If the agent forgets — e.g., the workflow that created the PR ended the turn at the PR URL without chaining into this skill — the loop must be invoked retroactively at the next opportunity:

- **Assume cycle 0 (the reviewer's initial review) already happened.** At
  session start / skill load, check whether the PR has *any* review activity
  from the configured reviewer.
- If that activity exists → do **not** wait for an initial review (it is
  already done); proceed straight to fetching threads and running the cycle.
- If there is **no** such activity anywhere on the PR → trigger the first
  review ourselves (cycle 1) and then wait. For a reviewer with
  `auto_reviews: false` this is the normal path, not a recovery path.
- This is a recovery clause only — skip entirely if no reviewer is configured
  for the repo.

## Follow-up Pushes After the Loop Stops

If the agent pushes new commits to a PR branch after the loop has already stopped:

- If any of those commits touch files where the reviewer left `UNRESOLVED` or `ADDRESSED_BY_REPLY` threads, automatically resume the loop (subject to the configured cap).
- Otherwise stay stopped. A self-reviewing bot will pick up the new commit unattended; a ping-only bot will not review again until asked, which is the intended stop.

Doc-only commits (README, CLAUDE.md, comments) never resume the loop on their own.

## Workflow

1. Trigger the loop by default after PR creation.
   - When the agent creates or opens a PR and this skill is available, continue into this workflow automatically unless the user explicitly says not to.
   - Treat "create the PR", "open a PR", "yeet this", "ship this PR", "run the AI reviewer loop", and "run the review loop" as permission to complete the full loop: wait, fetch, acknowledge, fix, verify, commit, push, and request reviewer re-review.

2. Resolve the PR.
   - If the user provides a PR URL, repo, or PR number, use it directly.
   - Otherwise, use the current git repository and branch:
     - `gh auth status`
     - `gh pr view --json number,url,headRefName,baseRefName`
   - If no PR exists for the branch, report that blocker.

3. Select the reviewer for this PR.
   - If a reviewer is already persisted, continue silently.
   - Otherwise run:
     `python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --list-reviewers --format json`
   - Use the **Reviewer Selection** rules above to confirm, pick, or stop. Persist confirmed choices with `--reviewer --reviewer-source confirmed`.

4. Wait for the configured reviewer to finish its first review.
   - **First check whether the reviewer starts on its own.** `--format json`
     reports `reviewerSelection.auto_reviews`. When it is `false` (Codex), no
     review will ever arrive unpushed: post the trigger first with
     `request_rereview.py --repo OWNER/REPO --pr N --json`, capture its
     `created_at` as `REREVIEW_AT`, and wait with `--after "$REREVIEW_AT"`.
     Skip that only when the PR already has review activity from that reviewer.
     Waiting without pinging a non-self-starting reviewer burns the whole
     timeout and reports a false "reviewer did not finish".
   - Otherwise run `scripts/fetch_gemini_threads.py --wait` from this skill.
   - **Cycle 1 (no `--after`):** the script returns as soon as reviewer
     activity is present — it does NOT wait for a quiet/settle period. The
     settle only matters on cycle 2+, where a freshly pushed fix needs the
     re-review to stabilize; pass `--after "$REREVIEW_AT"` there (step 11).
   - By default, the script resolves unresolved outdated reviewer threads after the wait and before returning current feedback.
   - If the wait times out, report that the reviewer did not finish within the timeout and do not invent feedback.

5. Check loop status and clean stale threads.
   - Count prior PR comments that ask the configured reviewer mention to review again.
   - If the count is already at or above the configured cap, do not start a new fix cycle and do not post a new re-review request unless the user raises the cap.
   - The cap does **not** block cleanup, metrics, final report output, stale/fixed-pending classification, or terminal recording.
   - Unresolved outdated reviewer threads and addressed-by-reply threads are resolved automatically unless the user opted out; stale threads should not drive new fixes.
   - For read-only inspection, pass `--no-resolve-outdated`.

6. Fetch reviewer threads.
   - The script uses the persisted reviewer. For a one-off explicit switch, pass `--reviewer <bot-login> --review-trigger-mention <known-mention> --reviewer-name <display-name>`.
   - Default thread filter: unresolved and not outdated.
   - Use JSON output when another tool or script will consume the result; use Markdown for human triage.

7. Acknowledge what needs to be fixed.
   - Before editing, briefly summarize the actionable reviewer findings grouped by file or behavior.
   - If there are no actionable unresolved threads, say so and stop after reporting the clean result.
   - **GATE — verification profile before any edit.** On the first run for this repo with no saved profile, set up the verification profile NOW (detect → preset menu → save) — *before* applying a single fix, so the verify strategy is fixed before edits. See the **Verification Profile** section. This is enforced mechanically: a bundled `PreToolUse` hook (`scripts/loop_profile_gate.py`) blocks `Edit`/`Write`/`MultiEdit` while a loop is active for the repo and no profile is saved. Saving any profile — including a deliberate **Skip** — clears the gate. If an edit is blocked, that is the signal you skipped this step: make the profile decision, then proceed.
   - After prefs/profile state is known, render and relay the profile intro:
     `python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --profile-intro --repo OWNER/REPO`

8. Classify comments.
   - Group by file and behavioral area.
   - Treat clear requested changes as actionable.
   - Ignore already resolved threads, outdated threads, approvals, duplicates, and informational comments.
   - If a thread asks for explanation rather than a code change, draft a response instead of forcing a code edit.
   - If comments conflict or could cause a behavioral regression, stop and surface the tradeoff.

9. Implement fixes.
   - **Do not edit until the step-7 profile gate is satisfied** (a profile is saved, even a `skipped` one). The `PreToolUse` hook will block edits otherwise.
   - Keep changes scoped to the reviewer feedback.
   - Read the relevant code before editing.
   - Preserve unrelated local changes.
   - Make each change traceable to a feedback cluster.

10. Verify.
   - Before running checks, render and relay the planned verification block:
     `python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --planned-verification --repo OWNER/REPO`
   - If a `confirmed`/`customized` profile exists for this repo, run
     `run_profile.py <owner/repo> <repo_root>` — it prints `to_details()` JSON
     and exits non-zero if a required check failed; feed its `verification` into
     `--verification` and the JSON into `--verification-details`
     (see "Verification Profile").
   - Otherwise (no profile, `skipped`, or unknown stack): run the narrowest
     meaningful checks first; broaden when shared logic or user-facing behavior
     changes.
   - If checks cannot run, report why and what remains unverified.

11. Commit, push, and request re-review.
    - For this skill's full loop, commit fixes to the PR branch. Push only after the required cycle receipt has been relayed for non-terminal cycles.
    - Use a clear commit message such as `fix: address AI reviewer findings`.
    - Before pushing a non-terminal cycle, emit the per-cycle receipt:
      `python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --cycle-summary --fixed-count <n-this-cycle> --verification <passed|failed|skipped>`
      then relay the printed `[loop] Cycle receipt` block verbatim. This is read-only — it does not write `runs.jsonl`.
    - Post the re-review request after a successful push only if this would not exceed the configured total re-review request cap.
    - Request re-review through the script-owned helper and parse JSON stdout. Pass the persisted safe trigger as `--review-trigger-mention`; if no safe trigger is known, pass `--no-safe-trigger --reviewer-login <login>` and relay the returned `status: no_safe_trigger` message exactly. Known reviewers need neither flag — the helper reads the persisted selection and posts that vendor's exact phrase (`@codex review` for Codex); never hand-write the trigger comment.
      ```bash
      python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/request_rereview.py" \
        --repo OWNER/REPO \
        --pr PR_NUMBER \
        --review-trigger-mention "$REVIEW_TRIGGER_MENTION" \
        --json
      ```
      Capture `created_at` from the JSON payload as `REREVIEW_AT`. If the payload has `status: no_safe_trigger`, stop without waiting and report the message.
    - Wait for the configured reviewer using chunked foreground waits — never a background wait:
      ```bash
      python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
        --wait \
        --after "$REREVIEW_AT" \
        --wait-chunk-seconds 60
      ```
      Statuses: `waiting` (no response yet), `settling` (the reviewer responded,
      quiet period running), `ready` (proceed — the same call returns the
      fetched threads), `timed_out` (total `--timeout` budget exhausted),
      `refused` (the reviewer declined outright). On `refused`, stop waiting
      immediately and relay the printed `[loop] STOP` block. Never keep waiting
      or re-ping — the reviewer already answered, and a retry burns a cycle.
      The payload's `kind` decides what happens next:
      - `kind: withdrawn` (the service is gone — sunset, no longer supported):
        terminal. Record with `--outcome human --outcome-reason 'reviewer
        refused the review' --gemini-unconfirmed`.
      - `kind: quota_exhausted` (the reviewer's usage cap is spent): the user
        can lift it, so ask them **now** — do not wait it out, do not record a
        terminal outcome yet. Prompt with the current runtime's choice-prompt
        mechanism (in Claude Code this may be `AskUserQuestion`; in Codex use
        the available user-input flow or ask one concise question directly),
        offering exactly two options: **Stop the loop** — record with
        `--outcome human --outcome-reason 'reviewer quota exhausted'
        --gemini-unconfirmed`; or **Upgrade / add credits, then retry** — after
        the user confirms they have done so, re-run the same chunked wait
        command with the same `--after "$REREVIEW_AT"`. Do not re-request the
        review: the re-review was already delivered and refused, and re-asking
        spends a cycle. Include the reviewer's own comment URL from the stop
        block so the user can reach the billing page.
      After each `waiting`/`settling` chunk: relay the printed `[loop]`
      heartbeat verbatim (markdown mode) or run `--wait-heartbeat` and relay
      its output (JSON mode), then immediately run the next chunk passing the
      script's `next_wait_seconds` as `--wait-chunk-seconds`. The script owns
      the 60s→90s decay; do not invent intervals.
      On `timed_out`, terminal recording uses `--gemini-unconfirmed`; do not
      guess `clean`, do not blindly mark `capped`, and allow
      `fixed_pending_confirmation`.
    - After the final wait has established confirmed/unconfirmed state, record the run exactly once:
      `python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --record-run --fixed-count <n> --verification <passed|failed|skipped> [--outcome <state>] [--gemini-confirmed|--gemini-unconfirmed]`
      then relay the printed `[loop] Summary` block verbatim.

## Script Usage

**`$GGRL_PLUGIN_ROOT` is the runtime-neutral plugin root.** Before running any
script command, resolve it once. Claude Code usually provides
`$CLAUDE_PLUGIN_ROOT`; Codex installs are discoverable from the Codex plugin
cache. Local development checkouts use the repo's `plugins/gh-review-loop`
folder.

```bash
GGRL_PLUGIN_ROOT="${GGRL_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$GGRL_PLUGIN_ROOT" ]; then
  GGRL_PLUGIN_ROOT=$(
    find ~/.codex/plugins ~/.codex/plugins/cache ~/.claude/plugins/cache \
      -type d -path "*/skills/gh-review-loop" 2>/dev/null \
      | sort -rV \
      | head -1 \
      | sed 's|/skills/gh-review-loop$||'
  )
fi
if [ -z "$GGRL_PLUGIN_ROOT" ] && [ -d "$(git rev-parse --show-toplevel 2>/dev/null)/plugins/gh-review-loop" ]; then
  GGRL_PLUGIN_ROOT="$(git rev-parse --show-toplevel)/plugins/gh-review-loop"
fi
export GGRL_PLUGIN_ROOT
```

This must be run as a Bash tool call first — do not inline it into the `python3` invocation. Once set, use it in subsequent calls.

From any repository with a GitHub PR:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py"
```

By default this resolves unresolved outdated reviewer threads AND addressed-by-reply
threads (unresolved threads where a non-bot maintainer posted a substantive
reply, >=30 chars) before printing current feedback. The re-review cap does not
block this cleanup.

Useful options:

```bash
# Wait for configured reviewer activity to appear (cycle 1 / initial review).
# No --after → returns as soon as activity is present; it does NOT wait for a
# quiet/settle period. At the initial review there either are comments or there
# aren't, so settling buys nothing — the wait only blocks until the reviewer has shown up.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --wait

# Wait for a NEW reviewer response after a re-review request (cycle 2+).
# Pass the re-review comment timestamp so prior-cycle activity is ignored.
# WITH --after the wait settles (waits for the new review to stabilize) before
# returning, so a fast-forward fetch doesn't catch a half-posted re-review.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --wait --after "$REREVIEW_AT"

# Chunked wait (preferred): return within 60s with a deterministic status
# instead of blocking. Relay the printed heartbeat verbatim, then run the next
# chunk with the suggested --wait-chunk-seconds. Never run the wait in the
# background.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --wait --after "$REREVIEW_AT" --wait-chunk-seconds 60

# After a --format json chunk, render the human heartbeat for relay:
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --wait-heartbeat

# Request reviewer re-review via the script-owned helper.
# Parse JSON stdout and capture `created_at` as REREVIEW_AT.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/request_rereview.py" \
    --repo OWNER/REPO \
    --pr PR_NUMBER \
    --review-trigger-mention "$REVIEW_TRIGGER_MENTION" \
    --json

# Render deterministic human-readable formatter blocks for relay.
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --profile-intro --repo OWNER/REPO
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --planned-verification --repo OWNER/REPO

# Read-only fetch (no GraphQL mutations)
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --no-resolve-outdated --no-resolve-addressed-by-reply

# Dry-run all resolutions (logs intended writes to stderr without calling GraphQL)
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --dry-run

# Specific PR URL
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --pr https://github.com/OWNER/REPO/pull/123

# JSON for automation
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --format json

# Include outdated, resolved, or addressed-by-reply threads while investigating history
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --no-resolve-outdated --include-outdated --include-resolved --include-addressed-by-reply

# Discover and persist a reviewer bot
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --list-reviewers --format json
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --reviewer coderabbitai --reviewer-source confirmed --reviewer-name CodeRabbit --review-trigger-mention @coderabbitai

# Reset reviewer selection for this PR
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --reset-reviewer
```

The script emits `warning: ... hit page limit ...` to stderr if any GraphQL page maxes out (review threads, reviews, PR comments, or comments within a thread), indicating older items may be silently missing.

## GitHub Write Safety

This skill's default full loop includes committing, pushing, asking the configured reviewer for re-review, and resolving outdated reviewer threads after PR creation or when the user asks for the review loop.

**Resolution policy:**

- **OUTDATED threads** — auto-resolved (line anchor no longer matches code).
- **ADDRESSED_BY_REPLY threads** — auto-resolved on the next pass when a non-bot maintainer has posted a substantive reply (>=30 chars). Implemented in `scripts/fetch_gemini_threads.py` via `is_addressed_by_reply` and resolved through the same GraphQL mutation as outdated threads. This prevents the same thread from re-tripping the loop forever after a deliberate deferral. Skip if the user has said "don't resolve" earlier in the session (pass `--no-resolve-addressed-by-reply`).
- **UNRESOLVED threads** — never resolved without an explicit "resolve" request from the user.
- **Reviews (approve/request-changes)** — never submitted unless explicitly asked.

For any uncertain run, prefer `--dry-run` first: the script logs `[dry-run] would resolve <kind> <thread-id>` to stderr without calling GraphQL. Useful when debugging the reply-detection heuristic against a real PR.

Stop before publishing if the fixes are ambiguous, tests expose a regression, local unrelated changes make it unsafe to commit cleanly, or the PR has already reached the configured re-review request cap.
