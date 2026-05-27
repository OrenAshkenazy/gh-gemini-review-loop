# gh-gemini-review-loop

**A fast-track Gemini Code Assist PR review loop for Claude Code. Thread-state-aware, severity-filtered, hard-capped at 3 cycles. No CI coupling.**

[![CI](https://github.com/OrenAshkenazy/gh-gemini-review-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/OrenAshkenazy/gh-gemini-review-loop/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/OrenAshkenazy/gh-gemini-review-loop?sort=semver)](https://github.com/OrenAshkenazy/gh-gemini-review-loop/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Different from [Gemini Code Assist's official GitHub Action](https://github.com/marketplace/gemini-code-assist) which runs in CI on push. This plugin runs **interactively from your dev environment**: Claude opens a PR, this skill closes the comments before you get up for coffee. No CI minutes spent. No org-wide rollout. Just install, work.

---

## Installation

This repo is a single-plugin Claude Code marketplace. Install in **two slash-commands**.

### Step 1 — Add the marketplace

In your Claude Code prompt:

```
/plugin marketplace add OrenAshkenazy/gh-gemini-review-loop
```

### Step 2 — Install the plugin

```
/plugin install gh-gemini-review-loop@gh-gemini-review-loop
```

That's it. The skill auto-triggers when you say things like *"handle Gemini feedback"*, *"run the Gemini loop"*, *"fix the review comments"*, or right after `gh pr create`.

To upgrade later:

```
/plugin marketplace update gh-gemini-review-loop
```

---

## Available plugins

### `gh-gemini-review-loop`

Run the full GitHub PR feedback loop with [Gemini Code Assist](https://github.com/apps/gemini-code-assist): wait for Gemini's review, fetch unresolved actionable threads, classify, fix, verify, commit, push, request re-review. Repeat up to a 3-cycle cap.

**Why it's different from the ~10 other Gemini-handling skills on GitHub:**

- **Thread-state-aware.** Uses GitHub's `reviewThreads` GraphQL (with `isResolved` / `isOutdated`) instead of the flat REST endpoint, so it actually knows what's actionable vs already-handled.
- **`ADDRESSED_BY_REPLY` detection.** Maintainer replied *"wontfix because X"*? The loop honors that — never re-tries the fix and auto-resolves the thread so it stops re-appearing every cycle.
- **Severity-aware ordering + filtering.** Parses Gemini's `critical` / `high` / `medium` / `low` priority markers. Sorts fixes by severity. Filter with `--min-severity high` to skip nits.
- **Hard 3-cycle cap, counted by the agent.** Only the agent's own re-review pings consume cycles (humans pinging Gemini don't burn cycles). Prevents runaway PR spam — a known failure mode of naive loops.
- **`--dry-run` for every write.** All GraphQL mutations route through one choke point that can log intended writes without executing.
- **Sticky receipt for background visibility.** `--sticky-receipt` posts one comment per PR that gets edited in place as the loop progresses, so PR watchers see live phase status (`RUNNING` → `DONE`) without comment spam.

**Quick usage examples** (Claude will pick the right flags from natural-language prompts):

| Say this to Claude | What runs |
|---|---|
| *"Run the Gemini loop"* | Full loop with defaults |
| *"Only fix high-severity Gemini findings"* | `--min-severity high` |
| *"Just audit Gemini comments, don't touch anything"* | `--dry-run --post-receipt --no-resolve-outdated --no-resolve-addressed-by-reply` |
| *"Be persistent, allow 4 cycles"* | `--max-rereview-requests 4` |
| *"Show a live status comment on the PR"* | `--sticky-receipt --receipt-status running` |

See [`SKILL.md`](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md) for the full workflow definition, stop conditions, and all variation phrasings.

---

## Prerequisites

- **Claude Code** (CLI / desktop / IDE) with plugin support enabled.
- **`gh` CLI** authenticated against the repos you'll run the loop on.
- **Python 3.10+** (uses PEP 604 `str | None` union syntax).
- **A repo where `gemini-code-assist` is configured as a reviewer** (the loop is opinionated for Gemini; doesn't aggregate other bots).

---

## Configuring the skill

Claude Code skills don't have a settings UI. Configure via three layers, in order of dominance:

1. **Natural-language prompts.** Say what you want; the agent picks the right script flags from [SKILL.md's Variations table](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md). This is the idiomatic path.
2. **`CLAUDE.md` preferences** for persistent per-user or per-repo defaults:
   ```markdown
   ## gh-gemini-review-loop preferences
   - Always pass --min-severity medium (we don't care about Gemini's low nits).
   - Always pass --post-receipt so we get an audit trail on every PR.
   - Use --max-rereview-requests 4 for the API repo (we want one extra cycle).
   ```
3. **Direct CLI flags** when invoking the script manually. See `--help` for the full list.

---

## Manual script invocation

You'll rarely need this — the skill drives the script for you — but it works:

```bash
# After /plugin install, the script lives under $CLAUDE_PLUGIN_ROOT
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --wait

# Notable flags:
python3 ... --dry-run                     # log writes without executing
python3 ... --post-receipt                # one-shot audit comment
python3 ... --sticky-receipt              # live, edited-in-place comment
python3 ... --min-severity high           # ignore Gemini's low/medium nits
python3 ... --drop-unknown-severity       # also ignore unmarked findings
python3 ... --no-resolve-outdated         # read-only inspection mode
python3 ... --include-resolved --include-outdated --include-addressed-by-reply   # full history
python3 ... --max-rereview-requests 4     # raise the 3-cycle cap
python3 ... --agent-login NAME            # override gh-detected agent login
python3 ... --author google-gemini-code-assist   # alternate bot login
```

Run `python3 .../fetch_gemini_threads.py --help` for the complete list.

---

## How it works (one-paragraph version)

The script queries GitHub's `pullRequest.reviewThreads` via GraphQL, filters to threads authored by `gemini-code-assist`, partitions them into four states (`RESOLVED` / `OUTDATED` / `ADDRESSED_BY_REPLY` / `UNRESOLVED`), and surfaces only the actionable subset. The agent fixes those, commits, pushes, then posts `@gemini-code-assist please review the latest changes.` once per cycle — counted strictly against the agent's own GitHub login so humans can ping Gemini freely. After 3 such cycles, hard stop. See the [Stopping Conditions](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md#stopping-conditions) section in SKILL.md for the full state machine.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the `release:*` label convention and the auto-release flow.
