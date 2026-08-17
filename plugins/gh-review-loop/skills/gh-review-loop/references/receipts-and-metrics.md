# Receipts, run metrics, and PR audit comments

The per-cycle discipline (`--cycle-summary` every non-terminal cycle, one terminal `--record-run`, relay blocks verbatim) is in SKILL.md. This file covers the PR-comment receipts, what metrics are stored, the semantic-risk note, and `--stats`.

## PR audit comments

**One-shot receipt (`--post-receipt`).** Leaves one audit-trail comment on the PR after the loop runs: cycles used, threads resolved (outdated + addressed-by-reply), threads still pending, severity breakdown of remaining actionable threads. Preview with `--dry-run --post-receipt`. Right for scripted/batch contexts where each invocation is independent.

**Sticky receipt (`--sticky-receipt`).** Maintains **one comment per PR that the script edits in place** across loop invocations — for long-running interactive loops where the user watches the PR tab, not the chat.

- First invocation posts a fresh comment with status `RUNNING` and stores its id in `~/.config/gh-gemini-review-loop/state.json` (override dir with `GGRL_STATE_DIR`).
- Subsequent invocations PATCH the same comment — no comment accretion.
- `--receipt-status {running,done,stopped}` sets the status header (default `RUNNING`). Tag the final invocation `done` (clean exit) or `stopped` (stop-condition) so the user sees the loop finished.
- Discovery fallback: if local state is missing, the script finds the receipt by its embedded marker and re-attaches.

## Run metrics (`runs.jsonl`)

`--record-run` appends one JSON record per completed loop to `~/.config/gh-gemini-review-loop/runs.jsonl` (append-only; never transmitted). Each record holds **counts only**: findings fetched/fixed/needs-human/addressed-by-reply, cycles used, verification result, outcome, duration, finding areas/paths, repo + PR number, and a judge-derived breakdown only when judge mode was on. **No identity is recorded** — no git author, no login — so the data cannot become a productivity score.

The run's start timestamp and per-finding accumulation reuse the same per-PR key in `state.json`; no extra state file.

## Semantic risk note

`--semantic-risk` is a manual/heuristic v1 signal — pass it (repeatable) when a fix changes behavior that passing tests may not fully cover: public signatures, return shapes, auth/security behavior, database queries, exception behavior, public APIs.

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" \
    --cycle-summary --fixed-count <n> --verification passed \
    --semantic-risk "hash_password(password) -> hash_password(password, salt)" \
    --semantic-risk "get_user() now returns one row instead of a list"
```

Relay the resulting `[loop] Semantic risk note (manual / heuristic)` block verbatim; do not present it as deterministic detection.

## `--stats` (read-only)

Aggregated stats for the current repo from `runs.jsonl`; never touches GitHub.

```bash
python3 "$GGRL_PLUGIN_ROOT/skills/gh-review-loop/scripts/fetch_gemini_threads.py" --stats
```

Options: `--stats-window N` (default 10 most-recent runs), `--stats-all-repos`, `--format json`.

Run metrics and `--stats` are local-only — stored under `~/.config/gh-gemini-review-loop/`, never posted to GitHub, no identity.
