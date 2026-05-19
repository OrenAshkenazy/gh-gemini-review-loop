# gh-gemini-review-loop

A Claude Code skill that runs the full PR feedback loop with [Gemini Code Assist](https://github.com/apps/gemini-code-assist) on GitHub:

1. Wait for Gemini's review.
2. Fetch unresolved actionable review threads (thread-aware, not flat comments).
3. Classify, fix, verify.
4. Commit, push, request re-review.
5. Repeat up to a 3-cycle cap.

See [`SKILL.md`](SKILL.md) for the full workflow definition and stopping conditions.

## Install

```bash
git clone https://github.com/OrenAshkenazy/gh-gemini-review-loop ~/.claude/skills/gh-gemini-review-loop
```

## Manual invocation

```bash
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --wait
```

See script `--help` for additional options.
