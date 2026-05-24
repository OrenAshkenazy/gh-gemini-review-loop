# gh-gemini-review-loop

A Claude Code skill that runs the full PR feedback loop with [Gemini Code Assist](https://github.com/apps/gemini-code-assist) on GitHub:

1. Wait for Gemini's review.
2. Fetch unresolved actionable review threads (thread-aware, not flat comments).
3. Classify, fix, verify.
4. Commit, push, request re-review.
5. Repeat up to a 3-cycle cap.

What this skill does that other Gemini-handling skills don't:

- **Thread-state-aware.** Uses GitHub's `reviewThreads` GraphQL (with `isResolved` / `isOutdated`) instead of flat REST comments, so it knows what's actually actionable.
- **`ADDRESSED_BY_REPLY` detection.** If a maintainer replied "wontfix because X", the loop never re-tries that fix — and auto-resolves the thread so it stops re-appearing.
- **Severity-aware ordering.** Parses Gemini's `critical` / `high` / `medium` / `low` priority markers and fixes high-severity threads first.
- **Hard 3-cycle cap.** Counted only against the agent's own re-review pings (humans pinging Gemini don't burn cycles). Prevents runaway PR spam.
- **`--dry-run`.** Every GraphQL mutation routes through one choke point that can log intended writes instead of executing them.
- **Loop receipt.** `--post-receipt` writes a one-comment audit trail to the PR.

See [`plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md`](plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/SKILL.md) for the full workflow definition and stopping conditions.

## Install (plugin)

Recommended path — this repo is a single-plugin marketplace.

```
/plugin marketplace add OrenAshkenazy/gh-gemini-review-loop
/plugin install gh-gemini-review-loop@gh-gemini-review-loop
```

The skill auto-triggers after `gh pr create` (or on prompts like "handle Gemini feedback" / "run the Gemini loop").

## Install (legacy / direct clone)

If you don't use the plugin system:

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

See `--help` for options. Notable flags: `--dry-run`, `--post-receipt`, `--no-resolve-outdated`, `--no-resolve-addressed-by-reply`, `--agent-login`, `--resolve-past-cap`.

## Requirements

- Python 3.10+ (uses PEP 604 `str | None` union syntax)
- `gh` CLI authenticated against the repo

## License

MIT. See [LICENSE](LICENSE).
