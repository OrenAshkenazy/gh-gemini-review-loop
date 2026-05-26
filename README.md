# gh-gemini-review-loop

**A fast-track Gemini Code Assist loop for Claude Code. No CI coupling, no GitHub Actions, no policy gates — just close the AI-reviewer feedback loop locally in seconds.**

> Different from [Gemini Code Assist's GitHub Action](https://github.com/marketplace/gemini-code-assist) which runs in CI on push. This skill runs interactively from your dev environment: Claude opens a PR, this skill closes the comments before you get up for coffee. No CI minutes spent. No org-wide rollout. Just install, work.

## When to use this skill

| Use it for | Don't use it for |
|---|---|
| Closing the loop on PRs you just opened, before merging | Org-wide enforcement of code review policy |
| Iterating quickly — "fix Gemini's high-severity stuff, ignore the nits" | Multi-bot review aggregation (CodeRabbit + Gemini + Copilot) |
| Headless / agent-driven PR workflows | Replacing human review |
| Personal productivity in Claude Code sessions | Running on every push (use [Gemini's official Action](https://github.com/marketplace/gemini-code-assist) for that) |

## What it does

1. Wait for Gemini's review.
2. Fetch actionable review threads via GraphQL (thread-aware, not flat REST comments).
3. Classify, fix, verify.
4. Commit, push, request re-review.
5. Repeat up to a 3-cycle cap.

## Why it's different from the ~10 other Gemini-handling skills on GitHub

- **Thread-state-aware.** Uses `reviewThreads` GraphQL (with `isResolved` / `isOutdated`) instead of the flat REST endpoint, so it actually knows what's actionable vs already-handled.
- **`ADDRESSED_BY_REPLY` detection.** Maintainer replied "wontfix because X"? The loop honors that — never re-tries the fix and auto-resolves the thread so it stops re-appearing every cycle.
- **Severity-aware ordering + filtering.** Parses Gemini's `critical` / `high` / `medium` / `low` priority markers. Sorts fixes by severity. Optionally filters with `--min-severity high` so you can skip the nits.
- **Hard 3-cycle cap, counted by the agent.** Only the agent's own re-review pings count toward the cap (humans pinging Gemini don't burn cycles). Prevents runaway PR spam — a known failure mode of naive loops.
- **`--dry-run` for every write.** All GraphQL mutations (`resolveReviewThread`, receipt comments) route through one choke point that can log intended writes without executing.
- **Loop receipt.** `--post-receipt` leaves one auditable summary comment on the PR: cycles used, threads resolved, severity breakdown, deferrals.

## Install

This repo is a single-plugin marketplace.

```
/plugin marketplace add OrenAshkenazy/gh-gemini-review-loop
/plugin install gh-gemini-review-loop@gh-gemini-review-loop
```

The skill auto-triggers after `gh pr create`, or on prompts like "handle Gemini feedback", "run the Gemini loop", "fix the review comments".

### Configuring the skill

Skills in Claude Code don't have a settings UI. Configure behavior in three ways:

1. **Natural-language prompts** — say what you want and the agent picks the right flags. See the [Variations table in SKILL.md](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md#variations-user-prompt--flag-mapping).
2. **CLAUDE.md preferences** — add persistent defaults to `~/.claude/CLAUDE.md` (user-global) or a per-repo `CLAUDE.md`:
   ```markdown
   ## gh-gemini-review-loop preferences
   - Always pass --min-severity medium (we don't care about nit comments).
   - Always pass --post-receipt so we get an audit trail.
   - Use --max-rereview-requests 4 in this repo.
   ```
3. **Direct CLI flags** — when invoking the script manually. See `--help` for the full list.

### Legacy install (no plugin system)

```bash
git clone https://github.com/OrenAshkenazy/gh-gemini-review-loop /tmp/ggrl
cp -r /tmp/ggrl/plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop \
      ~/.claude/skills/
```

## Manual script invocation

```bash
# Plugin install
python3 "$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py" --wait

# Legacy install
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --wait
```

Notable flags:

| Flag | Effect |
|---|---|
| `--dry-run` | Logs all intended GraphQL writes to stderr without executing them. |
| `--post-receipt` | Posts an audit trail comment to the PR when done. |
| `--min-severity {critical,high,medium,low}` | Drop actionable threads below the chosen severity. |
| `--drop-unknown-severity` | With `--min-severity`, also drop threads with no Gemini priority marker. |
| `--no-resolve-outdated` | Skip auto-resolution of outdated threads (read-only mode). |
| `--no-resolve-addressed-by-reply` | Skip auto-resolution of deferral-by-reply threads. |
| `--include-{resolved,outdated,addressed-by-reply}` | Show normally hidden threads in actionable output (for investigation). |
| `--max-rereview-requests N` | Override the 3-cycle cap. |
| `--agent-login NAME` | Override the auto-detected gh user for cycle counting. |
| `--author NAME` | Bot login to filter on (default: `gemini-code-assist`). |

## Requirements

- Python 3.10+ (uses PEP 604 `str | None` union syntax)
- `gh` CLI authenticated against the repo
- A repo where `gemini-code-assist` is a configured reviewer

## Non-goals

- Multi-bot review aggregation. By design — see [the rationale](https://github.com/OrenAshkenazy/gh-gemini-review-loop/issues) for why mixing bots hurts determinism.
- CI integration. This skill is for the local dev loop. If you want Gemini to gate merges, use the official [Gemini Code Assist GitHub Action](https://github.com/marketplace/gemini-code-assist) — it's a different tool for a different job.
- Replacing human review. Always pair with one.

## License

MIT. See [LICENSE](LICENSE).
