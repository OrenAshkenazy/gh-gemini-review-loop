# Reviewer-agnostic review loop with prompt-first selection

**Issue:** [#58 — PRD: Reviewer-author agnostic AI review loop](https://github.com/OrenAshkenazy/gh-gemini-review-loop/issues/58)
**Date:** 2026-06-28
**Status:** Approved design

## Intent

Keep today's behavior intact while letting an end user point the loop at any AI
reviewer bot — not just Gemini Code Assist — and give a **prompt-first
experience** for choosing the reviewer, mirroring the existing
verification-profile first-run menu.

In one line: *same loop, any reviewer, picked once via a friendly first-run
prompt and then remembered.*

This is intentionally small (PRD v1 scope). It is **not** a provider-adapter
framework, multi-reviewer aggregation, severity-parser registry, or a directory
rename. Gemini Code Assist remains the bundled **default adapter**, not a
hardcoded identity.

## Background (current state)

- The whole pipeline already keys off a single reviewer login, `args.author`,
  defaulting to `gemini-code-assist`. Threads are filtered by that login
  throughout `fetch_gemini_threads.py`.
- Per-PR state already exists in `state.json`, keyed by `owner/repo#number`,
  with an `entry` dict (`run`, `wait`, …) read/written via
  `load_sticky_state` / `save_sticky_state`.
- `request_rereview.py` builds the re-review trigger phrase; default mention is
  `@gemini-code-assist`.
- In the GraphQL `reviewThreads` data the script uses, GitHub App reviewers
  appear with a **bare login** (`gemini-code-assist`, `coderabbitai`) and
  `__typename: "Bot"`. That is why `DEFAULT_AUTHOR` has no `[bot]` suffix.
  Discovery must classify on `__typename`, not on a `[bot]` string suffix.
- The README is already rebranded (`gh-ai-review-loop`, "configured AI
  reviewer", Gemini as default adapter); some flags it mentions
  (`--reviewer-name`, `--review-trigger-mention`) are ahead of the code. Today
  only `--author` is wired into argparse.

## Design

### New module: `reviewer_resolver.py`

Keeps the 3015-line core from growing. Pure functions plus small helpers:

- `discover_candidates(pull_request, *, self_login) -> list[Candidate]`
  Returns one entry per distinct author of a **review thread** whose
  `__typename == "Bot"`, excluding `self_login` (the agent) and any human
  author. Fallback heuristic when `__typename` is absent (older fixtures /
  REST-shaped data): `login.endswith("[bot]")`. Deterministic order:
  first-thread appearance, then login. `Candidate = {login, display_name,
  review_trigger}` where `review_trigger` is filled from the known registry when
  available, else `None`.

- `KNOWN_TRIGGERS: dict[str, str] = {"gemini-code-assist": "@gemini-code-assist"}`
  The only safe-trigger registry in v1.

- `KNOWN_DISPLAY_NAMES: dict[str, str] = {"gemini-code-assist": "Gemini Code Assist"}`
  Display name fallback derives a readable name from the login otherwise.

- `make_reviewer_record(login, *, display_name=None, review_trigger=None, source) -> dict`
  Builds the persisted record (schema below), filling display name / trigger
  from the registries when not given.

Discovery requires adding `__typename` to the `author { login }` selections in
the existing GraphQL queries (review-thread comment authors). This is additive
and does not change existing parsing.

### Persisted reviewer state

A new key on the existing per-PR `state.json` entry:

```json
"reviewer": {
  "login": "gemini-code-assist",
  "display_name": "Gemini Code Assist",
  "review_trigger": "@gemini-code-assist",
  "source": "explicit | persisted | confirmed",
  "selected_at": "2026-06-28T12:00:00Z"
}
```

- No migration: a missing `reviewer` key means "not selected yet" and triggers
  the first-run prompt path.
- `source` records how it was chosen: `confirmed` (user accepted the first-run
  menu), `explicit` (`--reviewer` / explicit `--author`), `persisted` (reused
  from a prior run — only used in-memory, not rewritten each run).
- `review_trigger` may be `null` when no safe mention is known (unknown bot).

### Reviewer resolution in `fetch_gemini_threads.py`

Resolution runs right after `pr = resolve_pr(args.pr)`. Precedence for the
effective reviewer login:

1. Explicit `--reviewer <login>` (or explicit `--author`) → select + persist
   (`source="explicit"`), merging any `--reviewer-name` / `--review-trigger-mention`.
2. Persisted `entry["reviewer"].login` → reuse silently (`source="persisted"`).
3. Otherwise fall back to `DEFAULT_AUTHOR` (`gemini-code-assist`) so direct
   standalone CLI use and existing tests keep working — **but** the loop is
   flagged "reviewer not yet confirmed" so the agent runs the first-run prompt.

The resolved login feeds the existing `args.author`; the resolved trigger feeds
the re-review path.

`--author` keeps `gemini-code-assist` shown as its default in `--help`, but
internally uses a sentinel so "explicitly passed" is distinguishable from
"defaulted". Existing direct-CLI behavior is unchanged.

### New CLI flags (on `fetch_gemini_threads.py`)

- `--reviewer <login>` — select + persist this reviewer for the PR (the explicit
  switch). Sets `args.author`.
- `--reviewer-name <name>` — persisted display name (defaults from registry or
  derived from login).
- `--review-trigger-mention <@mention>` — persisted safe re-review mention.
- `--list-reviewers` — discover candidates, print them (text + `--format json`),
  exit 0 **without mutating state**. This is what the agent calls to build the
  menu.
- `--reset-reviewer` — delete the persisted `reviewer` key for the PR and exit 0,
  allowing rediscovery on the next run.

### First-run prompt (agent-driven, via SKILL.md)

Mirrors the verification-profile preset menu. On a PR with no persisted
reviewer:

1. Agent runs `--list-reviewers`.
2. Agent presents a short menu:
   - **0 candidates:** stop message — "No AI reviewer threads found on this PR."
     No selection persisted.
   - **1 candidate** (e.g. Gemini): one-time confirm — *"Use **Gemini Code
     Assist** as the reviewer for this PR? [Confirm · Pick another · None]"* —
     then persist with `--reviewer`.
   - **2+ candidates:** pick-one menu listing each discovered bot; persist the
     choice with `--reviewer`.
3. Returning runs read the persisted reviewer and are **prompt-free**. A new bot
   commenting later does **not** silently switch the source; the user must
   `--reviewer` (switch) or `--reset-reviewer` (rediscover).

The script never blocks on a TTY; the agent owns the interaction, exactly like
verification profiles. The one-time confirm applies even to a single Gemini
candidate (deliberate prompt-first experience), after which it is silent.

### Re-review trigger safety

In the re-review path (and `request_rereview.py`), the mention resolves as:
`--review-trigger-mention` → persisted `review_trigger` →
`KNOWN_TRIGGERS[login]` → `None`.

If it resolves to `None` (unknown bot, no override), the loop **does not post a
re-review** and emits a clear message: *"No safe re-review trigger known for
`<login>`; pass --review-trigger-mention to enable re-review requests."* It
never derives `@login` from the bot name and never posts a guessed mention.

### Naming / copy

Scripts and user-facing messages become reviewer-agnostic. SKILL.md gains a
"Reviewer selection" section and adjusts copy that is outright Gemini-specific.
No file or script-path renames (`fetch_gemini_threads.py` stays). Gemini
references remain as default-adapter / compatibility language.

## Components & boundaries

| Unit | Responsibility | Depends on |
|---|---|---|
| `reviewer_resolver.py` | discover candidates, registries, build reviewer record | PR thread data shape only |
| `fetch_gemini_threads.py` (resolution block) | precedence, persistence, CLI flags, wire into `args.author` + trigger | `reviewer_resolver`, sticky state |
| `request_rereview.py` | refuse to post when no safe trigger | — |
| SKILL.md | first-run menu wording + when to call which flag | the CLI flags above |

## Testing

New `tests/test_reviewer_resolver.py`:
- 0 / 1 / 2+ bot candidates discovered.
- `__typename == "Bot"` classification; humans excluded; agent's own login
  excluded; `[bot]`-suffix fallback when typename absent.
- known vs unknown trigger resolution.

Additions to `tests/test_fetch_gemini_threads.py`:
- persisted reviewer prevents silent switching when another bot appears later.
- explicit `--reviewer` switch persists the new reviewer.
- `--reset-reviewer` clears the saved reviewer and allows rediscovery.
- `--list-reviewers` prints candidates without mutating state.
- Gemini-only PR with no state: back-compat (Gemini selectable/auto-default).
- no implicit Gemini fallback when Gemini did not author review threads.
- unknown bot with review threads appears as a selectable candidate.

Additions to `tests/test_request_rereview.py`:
- missing safe re-review trigger → does not post, returns the stop message.

Run the full existing suite; it must stay green (no reintroduced Gemini
coupling, no breakage of current Gemini behavior).

## Out of scope (v1)

Multi-reviewer aggregation; provider-specific severity parsers; automatic
reviewer switching; GitHub App installation metadata as source of truth;
renaming installed plugin directories or script paths.
