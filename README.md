# gh-gemini-review-loop

**A fast-track Gemini Code Assist PR review loop for Claude Code. Thread-state-aware, severity-filtered, hard-capped at 3 cycles. No CI coupling.**

[![CI](https://github.com/OrenAshkenazy/gh-gemini-review-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/OrenAshkenazy/gh-gemini-review-loop/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/OrenAshkenazy/gh-gemini-review-loop?sort=semver)](https://github.com/OrenAshkenazy/gh-gemini-review-loop/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Different from [Gemini Code Assist's official GitHub Action](https://github.com/marketplace/gemini-code-assist) which runs in CI on push. This plugin runs **interactively from your dev environment**: Claude opens a PR, this skill closes the comments before you get up for coffee. No CI minutes spent. No org-wide rollout. Just install, work.

---

## Skill calibration (is this loop worth your money?)

Empirical answers, refreshed weekly by a cross-vendor LLM-as-judge eval on this repo's calibration corpus. See [`evals/results/latest.md`](evals/results/latest.md) for the current numbers and the full disagreement list.

- **Hand-labeled useful-rate on this repo's corpus:** ~91% (10 useful / 11 findings across PRs #6–#9). One false positive (CHANGELOG-context misread).
- **Severity recommendation:** the corpus suggests defaulting to `--min-severity medium`. The single false positive in the corpus was `medium`; no `low` findings have appeared yet, so `low` is unproven. Drop unmarked findings with `--drop-unknown-severity` if you only trust explicitly-graded ones.
- **Cycle economy** (observed across 13 PRs in this repo's own dogfooding): about 60% converged in 1 cycle, 30% in 2 cycles, 10% needed all 3. Defaulting `--max-rereview-requests 1` is the cheapest setting; bump to 3 if Gemini's findings tend to expose adjacent issues on your codebase.
- **Cost per loop** (Claude tokens dominate; Gemini and the GitHub API are free): on Sonnet ≈ $0.15–0.40 per cycle, ≈ $0.45–1.20 for a full 3-cycle loop. On Opus ≈ 5× that. On Haiku ≈ 1/3 of Sonnet.

The eval that produces these numbers is the maintainer's calibration tool — it does not ship to your install. End-users get the **distilled policy guidance above**; the OpenAI judge runs on this repo only.

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

To upgrade later (Claude Code uses `/plugin update` for installed plugins, distinct from `/plugin marketplace add` which only manages catalogs):

```
/plugin update gh-gemini-review-loop
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

You'll rarely need this, the skill drives the script for you, but it works for debugging.

`$CLAUDE_PLUGIN_ROOT` is set **only inside Claude Code's plugin runtime** — it isn't exported to your interactive shell, so the variable-form below only works from a Claude session (e.g., a Bash tool call). To run from a plain terminal, locate the cached script path yourself:

```bash
# From a plain terminal — find the installed path (handles version bumps):
SCRIPT=$(find ~/.claude/plugins/cache/gh-gemini-review-loop -name fetch_gemini_threads.py 2>/dev/null | sort | tail -1)
[ -n "$SCRIPT" ] && python3 "$SCRIPT" --wait || echo "Error: fetch_gemini_threads.py not found — install the plugin first." >&2

# From inside a Claude Code session (the env var IS populated there):
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --wait

# Notable flags:
python3 "$SCRIPT" --dry-run                     # log writes without executing
python3 "$SCRIPT" --post-receipt                # one-shot audit comment
python3 "$SCRIPT" --sticky-receipt              # live, edited-in-place comment
python3 "$SCRIPT" --min-severity high           # ignore Gemini's low/medium nits
python3 "$SCRIPT" --drop-unknown-severity       # also ignore unmarked findings
python3 "$SCRIPT" --no-resolve-outdated         # read-only inspection mode
python3 "$SCRIPT" --include-resolved --include-outdated --include-addressed-by-reply   # full history
python3 "$SCRIPT" --max-rereview-requests 4     # raise the 3-cycle cap
python3 "$SCRIPT" --agent-login NAME            # override gh-detected agent login
python3 "$SCRIPT" --author google-gemini-code-assist   # alternate bot login
```

Run `python3 "$SCRIPT" --help` for the complete list.

---

## Optional: per-finding OpenAI judge (`--judge-mode`)

End-users can opt into an OpenAI-powered judge that labels each Gemini finding as `valid_actionable` / `false_positive` / `needs_human` / `explanation_only` / `duplicate` / `already_addressed`, plus a `severity_override` and `recommended_action`. The label appears next to each finding in the loop output.

- **Default: off.** Nothing is sent to OpenAI until you opt in.
- **Privacy boundary:** when enabled, finding bodies + diff hunks are sent to the OpenAI API. The judge is read-only — it never resolves threads, posts comments, or pushes.
- **Cost:** `gpt-4o-mini` ≈ $0.001 per finding. Typical `on_complete` run ≈ $0.005 per PR.
- **Discoverability:** on your first loop with findings, the agent shows a one-time tip: `[loop] Tip: judge eval can give a second opinion on these findings.`
- **Natural language:** say "run the Gemini loop with judge eval at completion" or "with judge eval on every cycle". Preference is saved automatically.
- **Explicit setup:** say "enable judge eval" to get a mode prompt with all options.
- **Requires:** `OPENAI_API_KEY` env var + `pip install openai`. Missing either → judge skips gracefully; loop continues unchanged.

**Setting `OPENAI_API_KEY` permanently (macOS — recommended):**
```bash
# Store once in Keychain (never sits in a plaintext file):
security add-generic-password -a "$USER" -s "openai-api-key" -w "sk-..."

# Add to ~/.zshrc so it's available to all apps including Claude Code:
echo 'export OPENAI_API_KEY=$(security find-generic-password -a "$USER" -s "openai-api-key" -w 2>/dev/null)' >> ~/.zshrc
```

See [SKILL.md → Optional Judge Eval](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md) for the full flow and verdict schema.

## How it works (BTS)

The script queries GitHub's `pullRequest.reviewThreads` via GraphQL, filters to threads authored by `gemini-code-assist`, partitions them into four states (`RESOLVED` / `OUTDATED` / `ADDRESSED_BY_REPLY` / `UNRESOLVED`), and surfaces only the actionable subset. The agent fixes those, commits, pushes, then posts `@gemini-code-assist please review the latest changes.` once per cycle — counted strictly against the agent's own GitHub login so humans can ping Gemini freely. After 3 such cycles, hard stop. See the [Stopping Conditions](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md#stopping-conditions) section in SKILL.md for the full state machine.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the `release:*` label convention and the auto-release flow.
