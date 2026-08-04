# gh-ai-review-loop

**Move faster through AI reviewer PR feedback from Claude Code or Codex.**

[![CI](https://github.com/OrenAshkenazy/gh-review-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/OrenAshkenazy/gh-review-loop/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/OrenAshkenazy/gh-review-loop?sort=semver)](https://github.com/OrenAshkenazy/gh-review-loop/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Built for solo builders and small teams who want review feedback handled while they stay in flow. Claude Code or Codex waits for your configured AI reviewer, reads real GitHub review-thread state, fixes actionable comments, verifies, pushes, and asks the reviewer to re-review. It stops at a configurable cap (default: 3 cycles) so it cannot spam your PR.

[Gemini Code Assist](https://github.com/apps/gemini-code-assist) is the bundled default adapter. The same loop can target compatible reviewer bots such as CodeRabbit, GitHub Copilot, Qodo, or Sourcery by configuring the review author login and re-review mention. This plugin turns reviewer comments into an interactive fix loop inside your coding agent: no dashboard hopping, no manual comment triage, no heavy process to adopt.

## What Makes It Stand Out

Everything below ships in the plugin today. Each row links to the section with details.

| Capability | What you get | Why naive loops can't |
|---|---|---|
| [Thread-state truth](#why-this-plugin) | Reads GitHub's `reviewThreads` GraphQL (`isResolved` / `isOutdated`) and honors maintainer *"wontfix"* replies (`ADDRESSED_BY_REPLY`) | Flat comment scrapers re-fix what's already handled, every cycle |
| [Pattern sweep](#pattern-sweep--fix-the-class-not-the-instance) | Clusters findings by kind, fixes the *unflagged* sibling instances too, and tracks recurrence across cycles | Fixing only flagged sites invites the same finding back next review |
| [Verification gate](#verification-profiles) | Your repo's own tests and linters must pass before any push — auto-detected, confirmed once | Most loops push unverified fixes and let CI find out |
| [AI-judge triage](#optional-per-finding-openai-judge) | Optional second model labels each finding (`false_positive`, `needs_human`, …) before you spend a cycle on it | Without triage, one hallucinated finding burns a whole cycle |
| [Any reviewer bot](#supported-reviewers) | Gemini and Codex fully wired in; CodeRabbit / Copilot / Qodo / Sourcery configurable; severities normalized to one scale | Alternatives hard-code a single bot and its comment format |
| [Hard safety rails](#why-this-plugin) | Cycle cap counted only against the agent's own pings, `--dry-run` choke point for every write, sweeps report before touching unflagged code | Runaway PR spam is the classic failure mode of review loops |
| [Audit trail + stats](#run-metrics--local-stats) | One live-edited status comment on the PR, per-run receipts, local per-repo stats (`--stats`) | Fire-and-forget loops leave no record of what was fixed or why |
| [Zero infrastructure](#prerequisites) | Stdlib Python + `gh` CLI. No server, no webhook, no SDK installs; metrics never leave your machine | Bot platforms want a backend, an account, and your code |

### Verification Profiles — the loop that checks its own work

The loop doesn't assume its fixes are correct; it verifies them, gating every change on your repository's own tests and linters before it pushes. Setup is a single menu pick; after that it runs silently.

```text
┌───────────────────────────────┬───────────────────────────────────────────────────────────────────────────────────────────┐
│ Zero-touch setup              │ A single menu pick on the first run. No config files and no schema to learn.              │
├───────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
│ Repo-aware                    │ Auto-detects repo framework (Python, Node, Rust & Go) for cycle acceptance tests.         │
├───────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
│ Regression-proof              │ Every fix is gated on your own tests and linters before it is pushed.                     │
├───────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
│ Set once, then silent         │ Returning runs are prompt-free and deterministic.                                         │
├───────────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────┤
│ Secure by design              │ Pinned commands are saved only on confirmation.                                           │
└───────────────────────────────┴───────────────────────────────────────────────────────────────────────────────────────────┘
```

On the first run in a repository, you choose from a short menu — **All detected · Tests only · Skip · Customize** — and you're set. See [Verification profiles](#verification-profiles) for details.

---

## Prerequisites

- **Claude Code or Codex** with plugin support enabled.
- **`gh` CLI** authenticated against the repos you'll run the loop on.
- **Python 3.10+**.
- **A repo where an AI reviewer posts GitHub review threads.** Gemini Code Assist works out of the box. Other compatible reviewer bots need their GitHub author login and re-review mention configured for the loop scripts.

## Installation

This repo publishes the same plugin for both Claude Code and Codex. Install it in the runtime you use, or install both.

### Agent Skills (`npx skills`)

If you want the shared skills install flow instead of the runtime-specific plugin commands, use `npx skills add` and target the agents you want.

```bash
npx skills add OrenAshkenazy/gh-review-loop -a claude-code codex
```

The `-a` flag selects the agents that your local skills installer knows about.
With `skills` CLI 1.5.11, this installs the skill for both Claude Code and
Codex. If `codex` does not appear in your picker, update the skills installer or
use the Codex plugin install flow below.

Omit the `-a` flags if you want the interactive picker to choose agents for you.

### Codex

Add the marketplace:

```bash
codex plugin marketplace add OrenAshkenazy/gh-review-loop
```

Install the plugin:

```bash
codex plugin add gh-gemini-review-loop@gh-gemini-review-loop
```

### Claude Code

Install from the Claude Code marketplace in **two slash-commands**.

#### Step 1 — Add the marketplace

In your Claude Code prompt:

```
/plugin marketplace add OrenAshkenazy/gh-review-loop
```

#### Step 2 — Install the plugin

```
/plugin install gh-gemini-review-loop@gh-gemini-review-loop
```

That's it. The skill is now available to Claude Code.

To upgrade later (Claude Code uses `/plugin update` for installed plugins, distinct from `/plugin marketplace add` which only manages catalogs):

```
/plugin update gh-gemini-review-loop
```

---

## See It Before You Install

![Terminal demo of gh-gemini-review-loop handling Gemini Code Assist feedback](docs/gh-gemini-review-loop-demo.gif)

The demo shows a full run in under a minute: the loop activates on a PR with 6 Gemini findings, detects the repo's framework and arms the verification gate (`uv run pytest` — no push unless tests pass), the judge filters out a false positive, fixes land cycle by cycle, and a semantic-risk change pulls the developer in only where needed. It closes with the audit trail — every finding traced to the commit that fixed it — and the aggregated per-repo loop stats (`--stats`): average cycles, time to terminal outcome, findings fixed, false positives avoided.


## Use It

From a repo with an open GitHub PR, say this to Claude Code or Codex:

> Run the AI reviewer loop

The agent will:

1. Wait for the configured AI reviewer to finish reviewing.
2. Fetch unresolved, actionable reviewer threads.
3. Ignore stale, resolved, duplicate, or already-addressed threads.
4. Fix clear issues.
5. Run relevant checks.
6. Commit and push to the PR branch.
7. Ask the reviewer to re-review.
8. Stop once the PR is clean, a human decision is needed, or the configured re-review cap has been used.

You can also use more specific prompts:

| Say this to the agent | What happens |
|---|---|
| *"Run the AI reviewer loop"* | Full loop with defaults |
| *"Run the Gemini loop"* | Full loop using the default Gemini Code Assist adapter |
| *"Only fix high-severity reviewer findings"* | Skips lower-severity findings |
| *"Just audit reviewer comments, don't touch anything"* | Read-only inspection |
| *"One cycle only"* | Fixes once, then stops |
| *"Show a live status comment on the PR"* | Maintains one edited status comment on the PR |
| *"Run the AI reviewer loop with judge eval at completion"* | After the loop stops, OpenAI classifies any remaining reviewer findings as fix / reply / ignore / escalate, so you know whether to keep working or stop |

The skill also triggers naturally when the agent opens a PR and you ask it to keep going, handle review feedback, fix reviewer comments, or request re-review.

---

## Why This Plugin

Run the full GitHub PR feedback loop with the AI reviewer you configure: wait for review activity, fetch unresolved actionable threads, classify, fix, verify, commit, push, request re-review. Repeat up to the configured cap (default: 3 cycles). [Gemini Code Assist](https://github.com/apps/gemini-code-assist) is the default adapter.

**Why this is safer than a naive review-comment scraper:**

- **Thread-state-aware.** Uses GitHub's `reviewThreads` GraphQL (with `isResolved` / `isOutdated`) instead of the flat REST endpoint, so it actually knows what's actionable vs already-handled.
- **`ADDRESSED_BY_REPLY` detection.** Maintainer replied *"wontfix because X"*? The loop honors that — never re-tries the fix and auto-resolves the thread so it stops re-appearing every cycle.
- **Severity-aware ordering + filtering.** Parses reviewer `critical` / `high` / `medium` / `low` priority markers when present. Sorts fixes by severity. Filter with `--min-severity high` to skip nits.
- **Configurable cycle cap, counted by the agent.** Only the agent's own re-review pings consume cycles (humans pinging the reviewer don't burn cycles). Prevents runaway PR spam — a known failure mode of naive loops.
- **`--dry-run` for every write.** All GraphQL mutations route through one choke point that can log intended writes without executing.
- **Sticky receipt for background visibility.** `--sticky-receipt` posts one comment per PR that gets edited in place as the loop progresses, so PR watchers see live phase status (`RUNNING` → `DONE`) without comment spam.

See [`SKILL.md`](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md) for the full workflow definition, stop conditions, and all variation phrasings.

### Pattern sweep — fix the class, not the instance

LLM reviewers expand: fix the two flagged instances of a pattern and the next review flags three more in files it hadn't looked at yet. The loop collapses that whack-a-mole into one cycle:

1. **Cluster.** Each cycle groups findings by a deterministic pattern signature, so the agent reasons about *kinds* of problems ("missing decode guard", "unwrapped OSError"), not a flat finding list. The cycle receipt shows a `Patterns (N):` section.
2. **Sweep.** For any pattern flagged at 2+ sites, the agent greps the PR's **changed files** for sibling instances the reviewer hasn't flagged yet, prints a sweep report (which extra sites, why), then fixes the whole family at once. Changed files only — the blast radius never leaves the PR's own diff.
3. **Converge.** Swept patterns are tracked across cycles. If one reappears after a sweep, the receipt calls it out (`⚠ RECURRED after sweep`) so you know the sweep missed a variant — an advisory signal, never a control-flow change.

Net effect: patterns get fixed once instead of dripping in over three review cycles.

### Supported reviewers

Two reviewers ship with full vendor knowledge — re-review trigger, priority format, and review behavior:

| Reviewer | Support | Re-review trigger | Severity format |
|---|---|---|---|
| Gemini Code Assist | Built-in default | `@gemini-code-assist please review…` (posted automatically) | `critical` / `high` / `medium` / `low` |
| Codex (`chatgpt-codex-connector`) | Built-in | `@codex review` | `P0`–`P3`, normalized to the shared scale |
| CodeRabbit, Copilot, Qodo, Sourcery, … | Configurable | Supplied via `--review-trigger-mention` | Parsed when present |

`--list-reviewers` discovers which bots are active on your PR; `--reviewer` persists your choice per PR. Severity normalization means `--min-severity high` behaves identically no matter which bot wrote the comment. Codex findings that live in the review body (no inline thread) are surfaced too, and dropped automatically once a newer Codex review supersedes them.

---

## Optional: Per-Finding OpenAI Judge

End users can opt into an OpenAI-powered judge that labels each Gemini finding as `valid_actionable` / `false_positive` / `needs_human` / `explanation_only` / `duplicate` / `already_addressed`, plus a `severity_override` and `recommended_action`. The label appears next to each finding in the loop output.

- **Default: off.** Nothing is sent to OpenAI until you opt in.
- **Privacy boundary:** when enabled, finding bodies and diff hunks are sent to the OpenAI API. The judge is read-only. It never resolves threads, posts comments, or pushes.
- **Cost:** `gpt-4o-mini` is about $0.001 per finding. A typical `on_complete` run is about $0.005 per PR.
- **Discoverability:** on your first loop with findings, the agent shows a one-time tip: `[loop] Tip: judge eval can give a second opinion on these findings.`
- **Natural language:** say "run the Gemini loop with judge eval at completion" or "with judge eval on every cycle". Preference is saved automatically.
- **Explicit setup:** say "enable judge eval" to get a mode prompt with all options.
- **Requires:** an OpenAI API key, resolved from one of [several sources](#setting-your-openai_api_key). No SDK install needed — the judge uses stdlib `urllib`, so any working Python 3.10+ is enough. Missing key → judge skips gracefully and the loop continues unchanged.

### Judge eval TL;DR

1. Set `OPENAI_API_KEY` once (see [Setting your API key](#setting-your-openai_api_key) below).
2. Say one of these to Claude Code or Codex:

   > *"Enable judge eval"* — the agent prompts you to pick a mode and saves it.
   >
   > *"Run the Gemini loop with judge eval at completion"* — saves and runs in one step.

That's it. The agent handles the config file.

### Configuring it yourself

Prefer editing a config file over talking to the agent? Edit (or create) `~/.config/gh-gemini-review-loop/preferences.json`:

```json
{
  "judge_mode": "on_complete",
  "judge_model": "gpt-4o-mini",
  "max_rereview_requests": 3
}
```

| `judge_mode` value | When the judge runs |
|---|---|
| `off` (default) | Never. Nothing is sent to OpenAI |
| `on_complete` | Once, after the loop finishes — cheapest signal |
| `on_cycle` | Every fix cycle — more frequent, ~3× the cost |

Set `max_rereview_requests` to change the persistent loop cap. The CLI flag `--max-rereview-requests` still wins for a single manual invocation.

**To reset:** delete the file. Next run uses `judge_mode: off` and `max_rereview_requests: 3`.

### Setting your `OPENAI_API_KEY`

The judge resolves the key from the first available source, in this order:

1. **`OPENAI_API_KEY` env var** — CI, power users, Claude Code `settings.json` `env` block.
2. **Dotfile** at `~/.config/gh-gemini-review-loop/.env` (chmod 600) — `OPENAI_API_KEY="sk-..."`.
3. **macOS Keychain** (`gh-gemini-review-loop` / `openai`).
4. **Linux Secret Service** (`secret-tool`, GNOME Keyring / KWallet).

Manual commands below use `$GGRL_PLUGIN_ROOT`; see
[Advanced: Manual Script Invocation](#advanced-manual-script-invocation) if
you need to resolve it outside an agent session.

**Recommended one-liner** — stores the key in the OS keystore (Keychain on macOS, Secret Service on Linux) or chmod-600 dotfile elsewhere. Survives shell reloads, no rc-file edits, no `ps` leakage:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/key_resolver.py" --set
# Or, non-interactively from a password manager:
op read 'op://Personal/OpenAI/api key' | \
  python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/key_resolver.py" --set --from-stdin
```

**Inspect / debug:**

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/key_resolver.py" --print-source
# → source: macos_keychain
#   key:    sk-abc...wxyz   (redacted)

python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/key_resolver.py" --clear
```

**Self-hosted endpoints** (Ollama / LiteLLM / LM Studio / enterprise gateway): also set `OPENAI_BASE_URL` and the judge will POST there instead of `api.openai.com`. Key-shape validation is bypassed automatically.

> **Migrating from earlier versions?** Older releases required `OPENAI_API_KEY` as a shell-exported env var plus `pip install openai`. Both still work, but `key_resolver.py --set` is the new recommended path and the SDK is no longer needed.

### Verify your setup

If judge eval isn't working, run the doctor — it diagnoses every common failure (missing key, placeholder injected by `~/.claude/settings.json`, network unreachable, wrong Python) and prints the exact fix:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/judge_doctor.py" --probe
```

See [SKILL.md -> Optional Judge Eval](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md) for the full flow and verdict schema.

### Verification profiles

The loop can detect a per-repo **verification profile** on first run — the exact
checks to run at the verify step (pytest/ruff, npm scripts, cargo, go test). On
the first cycle with actionable findings, the agent scans the repo for signals
(pyproject.toml, package.json, Cargo.toml, go.mod) and presents a short **preset
menu**: *All detected*, a narrower *Tests only* / *First check only* (multi-check
repos), *Skip — use ad-hoc verification*, and *Customize manually*. Picking a
preset persists it under `profiles["owner/repo"]` in
`~/.config/gh-gemini-review-loop/preferences.json`; later runs skip the prompt and
run the saved checks. In v1 every saved check is a required gate — failure flips
the verify step to `--verification failed`. **Skip is remembered**: it suppresses
the automatic prompt and stays on ad-hoc checks, but saying *"set up a
verification profile for this repo"* re-runs detection and overrides it.

Example profile entry in `preferences.json`:

```json
{
  "profiles": {
    "owner/repo": {
      "detected_stack": "python",
      "source": "confirmed",
      "working_directory": ".",
      "timeout_seconds": 300,
      "checks": [
        {"name": "tests", "command": "pytest", "required": true},
        {"name": "lint", "command": "ruff check .", "required": true},
        {"name": "typecheck", "command": "mypy .", "required": true}
      ]
    }
  }
}
```

### Run metrics & local stats

Every completed loop run prints a one-screen receipt and appends a local record. Just ask:

> *"Show Gemini loop stats for this repo"*

(or run `--stats` directly) to aggregate those records per repo.

**Per-run receipt** (printed at loop end via `--record-run`, and per cycle via `--cycle-summary`). A small always-on core plus lines that appear only when they carry signal:

```
[loop] Summary
Findings fetched: 7
Fixed: 4
Remaining actionable: 0
Needs human: 1
Cycles used: 2/3
Verification: passed
Outcome: clean
Time to clean PR: 12m
```

**Aggregated stats** — *"Show Gemini loop stats for this repo"* / `--stats`:

```
Gemini loop stats — OrenAshkenazy/gh-review-loop
Last 10 runs

Average cycles used: 1.8
Average elapsed time to terminal outcome: 9m
Average elapsed time to clean PR: 7m
Average elapsed time to capped run: 31m
Average elapsed time to failed run: 4m
Average active cycle time: 3m
Average active time per run: 6m
Average cycles per run: 1.5
Findings fixed: 32 of 41
Human decisions needed: 6
Addressed by reply: 9
False positives avoided: 14   (across 6 of 10 judged runs)
Most common provider: gemini-code-assist
Most repeated finding area: tests
```

Two kinds of time are reported separately:

- **Elapsed** — user-visible wall-clock latency (includes review-bot waits, polling, and idle time), split by terminal outcome (clean / capped / failed).
- **Active** — agent/loop processing time only, excluding waits.

Metrics are local-only (`~/.config/gh-gemini-review-loop/runs.jsonl`), contain no identity (repo and PR number only), and are never transmitted. Judge-derived and conditional lines (e.g. "False positives avoided", "Addressed by reply", the active-cycle lines) appear only when that data exists for the runs in range.

---

## Configuring the skill

Agent skills do not have a runtime settings UI. Persistent script settings live in `~/.config/gh-gemini-review-loop/preferences.json`.

### Set the loop cap

The default cap is `3` re-review requests per PR. The script reads `max_rereview_requests` from `~/.config/gh-gemini-review-loop/preferences.json` on every invocation. To make the loop stop after 4 re-review requests by default, create or edit that file:

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

The file should contain this key:

```json
{
  "schema_version": 1,
  "max_rereview_requests": 4
}
```

You can keep judge eval settings in the same file. The script preserves `max_rereview_requests` when saving judge settings.

For one run only, use the CLI flag instead:

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --max-rereview-requests 4
```

Override precedence, highest first:

1. **Direct CLI flags** when invoking the script manually. See `--help` for the full list. CLI flags override the preferences file.
2. **Natural-language prompts.** Say what you want; the agent picks the right script flags from [SKILL.md's Variations table](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md). This is the idiomatic path.
3. **`CLAUDE.md` preferences** for persistent per-user or per-repo defaults:
   ```markdown
   ## gh-gemini-review-loop preferences
   - Always pass --min-severity medium (we don't care about Gemini's low nits).
   - Always pass --post-receipt so we get an audit trail on every PR.
   - Use --max-rereview-requests 4 for the API repo (we want one extra cycle).
   ```
4. **Preferences file** for persistent script-level defaults. This is the recommended place for the cap:
   ```json
   {
     "max_rereview_requests": 4
   }
   ```

For the optional OpenAI judge, see [Judge eval TL;DR](#judge-eval-tldr). It shares the same prefs file at `~/.config/gh-gemini-review-loop/preferences.json`.

---

## Advanced: Manual Script Invocation

You'll rarely need this, the skill drives the script for you, but it works for debugging.

For manual commands, resolve `$GGRL_PLUGIN_ROOT` once. Claude Code usually
provides `$CLAUDE_PLUGIN_ROOT`; Codex installs are discoverable from the Codex
plugin cache. Local development checkouts use the repo's
`plugins/gh-gemini-review-loop` folder.

```bash
GGRL_PLUGIN_ROOT="${GGRL_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$GGRL_PLUGIN_ROOT" ]; then
  GGRL_PLUGIN_ROOT=$(
    find ~/.codex/plugins ~/.codex/plugins/cache ~/.claude/plugins/cache \
      -type d -path "*/skills/gh-gemini-review-loop" 2>/dev/null \
      | sort -rV \
      | head -1 \
      | sed 's|/skills/gh-gemini-review-loop$||'
  )
fi
if [ -z "$GGRL_PLUGIN_ROOT" ] && [ -d "$(git rev-parse --show-toplevel 2>/dev/null)/plugins/gh-gemini-review-loop" ]; then
  GGRL_PLUGIN_ROOT="$(git rev-parse --show-toplevel)/plugins/gh-gemini-review-loop"
fi
export GGRL_PLUGIN_ROOT

python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --wait

# Notable flags:
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --dry-run
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --post-receipt
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --sticky-receipt
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --min-severity high
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --drop-unknown-severity
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --no-resolve-outdated
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --include-resolved --include-outdated --include-addressed-by-reply
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --max-rereview-requests 4
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --agent-login NAME
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --list-reviewers --format json
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --reviewer coderabbitai --reviewer-name CodeRabbit --review-trigger-mention @coderabbitai
python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --reset-reviewer
```

Run `python3 "$GGRL_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --help` for the complete list.

---

## How It Works

The script queries GitHub's `pullRequest.reviewThreads` via GraphQL, filters to threads authored by the configured reviewer login, partitions them into four states (`RESOLVED` / `OUTDATED` / `ADDRESSED_BY_REPLY` / `UNRESOLVED`), and surfaces only the actionable subset. The agent fixes those, commits, pushes, then posts the configured re-review mention once per cycle. Gemini Code Assist is the bundled default; `--list-reviewers` discovers reviewer bot candidates, `--reviewer` persists the chosen reviewer for the PR, and `--review-trigger-mention` enables safe re-review requests for non-default bots. Re-review requests are counted strictly against the agent's own GitHub login so humans can ping the reviewer freely. After the configured cap is reached, hard stop. See the [Stopping Conditions](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md#stopping-conditions) section in SKILL.md for the full state machine.

---

## License

MIT. See [LICENSE](LICENSE).

---

## What I Want Feedback On

- **Install friction:** where the Claude Code marketplace flow, `gh` auth, or Python dependency expectations feel unclear.
- **False positives:** whether Gemini findings that survive filtering are usually worth acting on.
- **Thread state handling:** whether `RESOLVED`, `OUTDATED`, `ADDRESSED_BY_REPLY`, and `UNRESOLVED` match how maintainers think about review comments.
- **Safety around resolving outdated threads:** whether auto-resolving stale Gemini threads is acceptable by default, or should be more conservative.
- **Whether judge eval is worth adding:** whether the optional OpenAI judge helps enough to justify the setup, privacy boundary, and small API cost.

---

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the `release:*` label convention and the auto-release flow.
