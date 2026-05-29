# gh-gemini-review-loop

**Move faster through Gemini Code Assist PR feedback from Claude Code.**

[![CI](https://github.com/OrenAshkenazy/gh-gemini-review-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/OrenAshkenazy/gh-gemini-review-loop/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/OrenAshkenazy/gh-gemini-review-loop?sort=semver)](https://github.com/OrenAshkenazy/gh-gemini-review-loop/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Built for solo builders and small teams who want review feedback handled while they stay in flow. Claude waits for Gemini, reads real GitHub review-thread state, fixes actionable comments, verifies, pushes, and asks Gemini to re-review. It stops after 3 cycles so it cannot spam your PR.

[Gemini Code Assist](https://github.com/apps/gemini-code-assist) gives you review comments. This plugin turns those comments into an interactive fix loop inside Claude Code: no dashboard hopping, no manual comment triage, no heavy process to adopt.

---

## Prerequisites

- **Claude Code** with plugin support enabled.
- **`gh` CLI** authenticated against the repos you'll run the loop on.
- **Python 3.10+**.
- **A repo where `gemini-code-assist` is configured as a reviewer.** This plugin is opinionated for Gemini Code Assist and does not aggregate other review bots.

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

That's it. The skill is now available to Claude Code.

To upgrade later (Claude Code uses `/plugin update` for installed plugins, distinct from `/plugin marketplace add` which only manages catalogs):

```
/plugin update gh-gemini-review-loop
```

---

## Use It

From a repo with an open GitHub PR, say this to Claude:

> Run the Gemini loop

Claude will:

1. Wait for Gemini Code Assist to finish reviewing.
2. Fetch unresolved, actionable Gemini review threads.
3. Ignore stale, resolved, duplicate, or already-addressed threads.
4. Fix clear issues.
5. Run relevant checks.
6. Commit and push to the PR branch.
7. Ask Gemini to re-review.
8. Stop once the PR is clean, a human decision is needed, or 3 re-review cycles have been used.

You can also use more specific prompts:

| Say this to Claude | What happens |
|---|---|
| *"Run the Gemini loop"* | Full loop with defaults |
| *"Only fix high-severity Gemini findings"* | Skips lower-severity findings |
| *"Just audit Gemini comments, don't touch anything"* | Read-only inspection |
| *"One cycle only"* | Fixes once, then stops |
| *"Show a live status comment on the PR"* | Maintains one edited status comment on the PR |
| *"Run the Gemini loop with judge eval at completion"* | After the loop stops, OpenAI classifies any remaining Gemini findings as fix / reply / ignore / escalate, so you know whether to keep working or stop |

The skill also triggers naturally when Claude opens a PR and you ask it to keep going, handle review feedback, fix Gemini comments, or request Gemini re-review.

---

## See It Before You Install

![Terminal demo of gh-gemini-review-loop handling Gemini Code Assist feedback](docs/gh-gemini-review-loop-demo.gif)

The demo shows the whole happy path: `gh pr create`, Gemini comments appearing, Claude running the loop, the thread-aware fetcher surfacing actionable feedback, fixes, tests, push, and a re-review request.

Want to replay it in your terminal instead of watching the GIF? The asciinema cast is checked into the repo:

```bash
asciinema play https://raw.githubusercontent.com/OrenAshkenazy/gh-gemini-review-loop/main/docs/gh-gemini-review-loop-demo.cast
```

From a cloned checkout, play the same 60-second terminal demo locally:

```bash
asciinema play docs/gh-gemini-review-loop-demo.cast
```

### Real Example Transcript

This is a shortened transcript from PR [#14](https://github.com/OrenAshkenazy/gh-gemini-review-loop/pull/14) in this repo. It shows the flow people should expect before they install the plugin.

```text
$ gh pr create --fill
https://github.com/OrenAshkenazy/gh-gemini-review-loop/pull/14

gemini-code-assist: 3 review comments
  - evals/run_eval.py: sort PR fixtures numerically
  - evals/run_eval.py: guard empty judge reasons
  - evals/judge.py: preserve valid falsy reason values

Claude Code:
  Run the Gemini loop

[loop] cycle 1/3 — fetching threads from PR #14...
$ python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --wait
# Gemini Code Assist Threads for PR #14
Re-review requests: 0
Threads: 3

## 1. evals/run_eval.py [medium]
Sort fixture names numerically so pr-10 does not sort before pr-6.

## 2. evals/run_eval.py [medium]
Guard empty judge reasons before rendering the disagreement summary.

## 3. evals/judge.py [medium]
Do not discard valid falsy reason values when parsing the judge response.

[loop] cycle 1/3 — 3 actionable thread(s) (medium: 3). Fixing.
[loop] cycle 1/3 — fixes applied. Verifying.
$ uv run pytest tests/test_judge.py evals/test_eval.py
44 passed

[loop] cycle 1/3 — committing and pushing 56a9443...
$ git push
[loop] cycle 1/3 — pushed. Requesting Gemini re-review (cycle 1 consumed).
$ gh pr comment 14 --body "@gemini-code-assist please review the latest changes."

gemini-code-assist reviewed the new commit: 2 medium hardening comments

[loop] cycle 2/3 — fetching threads from PR #14...
[loop] cycle 2/3 — 2 actionable thread(s) (medium: 2). Fixing.
$ uv run pytest tests/test_judge.py evals/test_eval.py
47 passed
$ git push && gh pr comment 14 --body "@gemini-code-assist please review the latest changes."

gemini-code-assist reviewed the latest commit:
  There are no review comments to assess, and I have no additional feedback.

[loop] DONE — 0 actionable threads remaining. Cycles used: 2/3.
```

---

## Why This Plugin

Run the full GitHub PR feedback loop with [Gemini Code Assist](https://github.com/apps/gemini-code-assist): wait for Gemini's review, fetch unresolved actionable threads, classify, fix, verify, commit, push, request re-review. Repeat up to a 3-cycle cap.

**Why this is safer than a naive Gemini comment scraper:**

- **Thread-state-aware.** Uses GitHub's `reviewThreads` GraphQL (with `isResolved` / `isOutdated`) instead of the flat REST endpoint, so it actually knows what's actionable vs already-handled.
- **`ADDRESSED_BY_REPLY` detection.** Maintainer replied *"wontfix because X"*? The loop honors that — never re-tries the fix and auto-resolves the thread so it stops re-appearing every cycle.
- **Severity-aware ordering + filtering.** Parses Gemini's `critical` / `high` / `medium` / `low` priority markers. Sorts fixes by severity. Filter with `--min-severity high` to skip nits.
- **Hard 3-cycle cap, counted by the agent.** Only the agent's own re-review pings consume cycles (humans pinging Gemini don't burn cycles). Prevents runaway PR spam — a known failure mode of naive loops.
- **`--dry-run` for every write.** All GraphQL mutations route through one choke point that can log intended writes without executing.
- **Sticky receipt for background visibility.** `--sticky-receipt` posts one comment per PR that gets edited in place as the loop progresses, so PR watchers see live phase status (`RUNNING` → `DONE`) without comment spam.

See [`SKILL.md`](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md) for the full workflow definition, stop conditions, and all variation phrasings.

---

## Optional: Per-Finding OpenAI Judge

End users can opt into an OpenAI-powered judge that labels each Gemini finding as `valid_actionable` / `false_positive` / `needs_human` / `explanation_only` / `duplicate` / `already_addressed`, plus a `severity_override` and `recommended_action`. The label appears next to each finding in the loop output.

- **Default: off.** Nothing is sent to OpenAI until you opt in.
- **Privacy boundary:** when enabled, finding bodies and diff hunks are sent to the OpenAI API. The judge is read-only. It never resolves threads, posts comments, or pushes.
- **Cost:** `gpt-4o-mini` is about $0.001 per finding. A typical `on_complete` run is about $0.005 per PR.
- **Discoverability:** on your first loop with findings, the agent shows a one-time tip: `[loop] Tip: judge eval can give a second opinion on these findings.`
- **Natural language:** say "run the Gemini loop with judge eval at completion" or "with judge eval on every cycle". Preference is saved automatically.
- **Explicit setup:** say "enable judge eval" to get a mode prompt with all options.
- **Requires:** `OPENAI_API_KEY` env var + `pip install openai`. Missing either means the judge skips gracefully and the loop continues unchanged.

**Setting `OPENAI_API_KEY` permanently on macOS:**

```bash
# Store once in Keychain (never sits in a plaintext file):
security add-generic-password -a "$USER" -s "openai-api-key" -w "sk-..."

# Add to ~/.zshrc so it's available to all apps including Claude Code:
echo 'export OPENAI_API_KEY=$(security find-generic-password -a "$USER" -s "openai-api-key" -w 2>/dev/null)' >> ~/.zshrc
```

See [SKILL.md -> Optional Judge Eval](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md) for the full flow and verdict schema.

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

## Advanced: Manual Script Invocation

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

## How It Works

The script queries GitHub's `pullRequest.reviewThreads` via GraphQL, filters to threads authored by `gemini-code-assist`, partitions them into four states (`RESOLVED` / `OUTDATED` / `ADDRESSED_BY_REPLY` / `UNRESOLVED`), and surfaces only the actionable subset. The agent fixes those, commits, pushes, then posts `@gemini-code-assist please review the latest changes.` once per cycle — counted strictly against the agent's own GitHub login so humans can ping Gemini freely. After 3 such cycles, hard stop. See the [Stopping Conditions](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md#stopping-conditions) section in SKILL.md for the full state machine.

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
