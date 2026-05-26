# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Progress Narration** section in SKILL.md. Requires the agent to emit one-line status updates (`[loop] cycle N/3 — <phase>`) at each phase transition during the loop. No code change — pure agent-instruction layer. Eliminates the "silent loop feels broken" UX problem in interactive sessions.
- `--min-severity {critical,high,medium,low}` flag and `filter_by_min_severity()` helper. Drops actionable threads below the chosen severity. By default, threads with no Gemini priority marker (`unknown`) are kept; pass `--drop-unknown-severity` to remove them too.
- `filter_by_min_severity` now accepts `min_severity=None`, enabling `--drop-unknown-severity` to be used independently of `--min-severity` (closes a silent no-op).
- **Variations** section in SKILL.md mapping common user phrasings ("only fix high severity", "audit-only run", "be persistent") to the right script flag combinations. Makes agent dispatch deterministic.
- 8 new pytest cases for `filter_by_min_severity` (covering each severity threshold, unknown handling, stability, empty input). Test count: 36 → 44.

### Changed

- README rewritten to lead with **fast-track Gemini loop, no CI coupling** positioning. New "When to use this skill" table contrasting against [Gemini's official GitHub Action](https://github.com/marketplace/gemini-code-assist). Adds a "Configuring the skill" section pointing users at CLAUDE.md preferences as the idiomatic config layer.

## [0.1.0] - 2026-05-24

First publishable release. Packaged as a Claude Code plugin marketplace.

### Added

- **Plugin packaging.** Repo is now a single-plugin marketplace (`.claude-plugin/marketplace.json`, `plugins/gh-gemini-review-loop/.claude-plugin/plugin.json`). Install via `/plugin marketplace add OrenAshkenazy/gh-gemini-review-loop` then `/plugin install gh-gemini-review-loop@gh-gemini-review-loop`.
- `ADDRESSED_BY_REPLY` detection and auto-resolution. Unresolved threads where a non-bot maintainer posted a substantive (≥30 char) reply are treated as deliberate deferrals; the loop never re-attempts those fixes and resolves the threads via GraphQL on the next pass.
- `--dry-run` flag. All GraphQL mutations (`resolveReviewThread`, `gh pr comment` for receipts) route through a single choke point that logs intended writes to stderr instead of executing.
- Pagination guard. Warns when `reviewThreads` / `reviews` / PR comments hit the 100-item page limit or when per-thread comments hit 50, surfacing silent data loss on large PRs.
- Severity-aware ordering. Parses Gemini's priority image markers (`critical` / `high` / `medium` / `low`) and orders actionable threads with critical first.
- Agent-scoped cycle counter. Auto-detects the agent's GitHub login via `gh api user` and only counts re-review requests posted by that login toward the 3-cycle cap. Humans pinging Gemini no longer consume cycles. Override with `--agent-login NAME`, opt out with `--no-agent-filter`.
- `--post-receipt` flag. Posts a one-comment audit trail to the PR: cycles used, threads resolved, threads pending, severity breakdown.
- Pytest suite (36 tests) covering all pure functions: `parse_pr_url`, `is_addressed_by_reply`, `filter_threads`, `thread_severity`, `sort_by_severity`, `severity_counts`, `rereview_requests`, `pagination_warnings`, `thread_fingerprint`, `render_receipt`.
- GitHub Actions CI: ruff + py_compile + pytest on Python 3.10 / 3.11 / 3.12.
- MIT LICENSE.

### Changed

- `--ignore-loop-limit` renamed to `--resolve-past-cap` (old name was misleading; only re-enabled resolution past the cap, didn't ignore the cap entirely). Old flag retained as a hidden deprecated alias.
- SKILL.md description tightened from ~440 chars to ~270 chars while preserving trigger phrases.
- Script path in SKILL.md uses `$CLAUDE_PLUGIN_ROOT/...` so the same skill works under both plugin install and legacy `~/.claude/skills/` install.

### Fixed

- Markdown renderer now displays the addressed-by-reply count alongside outdated and actionable counts.

[Unreleased]: https://github.com/OrenAshkenazy/gh-gemini-review-loop/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OrenAshkenazy/gh-gemini-review-loop/releases/tag/v0.1.0
