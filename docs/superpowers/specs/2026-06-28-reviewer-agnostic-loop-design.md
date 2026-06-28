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
  reviewer", Gemini as default adapter). This design completes the script wiring
  for the reviewer-agnostic flags and first-run reviewer selection.

## Design

### New module: `reviewer_resolver.py`

Keeps the 3015-line core from growing. Pure functions plus small helpers:

- `discover_candidates(pull_request, *, self_login) -> list[Candidate]`
  Returns one entry per distinct author of a **review-thread comment** whose
  `author.__typename == "Bot"`, excluding `self_login` (the agent) and any
  human author. `self_login` comes from the same source used for re-review cap
  attribution: explicit `--agent-login` when provided, otherwise
  `gh_authenticated_login()` (`gh api user --jq .login`), falling back to no
  self-exclusion if the login cannot be resolved. Fallback heuristic when
  `__typename` is absent (older fixtures / REST-shaped data):
  `login.endswith("[bot]")`. Deterministic order:
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

`--list-reviewers` must not rely on the normal single-page fetch when deciding
that a PR has no candidates. It uses a discovery fetch that paginates
`reviewThreads` and nested thread comments until `hasNextPage == false`, or
until an explicit implementation cap is reached. If the cap is reached, the
command prints a warning and marks the result as partial in JSON; the agent must
not turn a partial zero-candidate result into "No AI reviewer threads found."

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

### Reviewer selection state contract

Reviewer resolution returns both the effective reviewer and a machine-readable
selection state. In `--format json`, the script includes:

```json
"reviewerSelection": {
  "login": "gemini-code-assist",
  "display_name": "Gemini Code Assist",
  "review_trigger": "@gemini-code-assist",
  "source": "default_unconfirmed | explicit | persisted | confirmed",
  "confirmation_required": true,
  "candidates_partial": false
}
```

- `default_unconfirmed` means the script applied `DEFAULT_AUTHOR` for
  compatibility, but no reviewer has been persisted for this PR. The agent must
  run the first-run reviewer prompt before edits or re-review requests.
- `explicit`, `confirmed`, and `persisted` are prompt-free for the current run.
- `candidates_partial` comes from `--list-reviewers`; if true, the agent must
  not claim that no AI reviewer exists on the PR.

Fresh PRs need special handling. If `--list-reviewers` returns 0 candidates and
the PR has no reviewer state, the agent must not immediately stop in the normal
post-PR workflow. It presents a three-way choice instead: use the bundled Gemini
default and wait for its first review, enter a reviewer login manually, or stop
with "No AI reviewer threads found on this PR." Direct standalone CLI behavior
still falls back to Gemini so existing scripts and tests keep working.

`--author` keeps `gemini-code-assist` shown as its default in `--help`, but
argparse stores `None` when the flag is omitted:

```python
parser.add_argument(
    "--author",
    default=None,
    help="Reviewer author login. Default: gemini-code-assist",
)
```

That gives the resolver a reliable "not explicit" signal without a custom
string sentinel. Existing direct-CLI behavior is unchanged because the resolver
still applies `DEFAULT_AUTHOR` after checking explicit and persisted choices.

### New CLI flags (on `fetch_gemini_threads.py`)

- `--reviewer <login>` — select + persist this reviewer for the PR (the explicit
  switch). Sets `args.author`.
- `--reviewer-source explicit|confirmed` — source marker to persist with
  `--reviewer`; the agent uses `confirmed` after the first-run prompt, while
  direct explicit switches default to `explicit`.
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
   - **0 candidates:** if this is the normal post-PR workflow with no persisted
     reviewer, offer **Use Gemini default and wait**, **Pick another**, or
     **None**. Only the **None** path stops with "No AI reviewer threads found
     on this PR." No selection is persisted on stop.
   - **1 candidate** (e.g. Gemini): one-time confirm — *"Use **Gemini Code
     Assist** as the reviewer for this PR? [Confirm · Pick another · None]"* —
     then persist with `--reviewer --reviewer-source confirmed`.
   - **2+ candidates:** pick-one menu listing each discovered bot; persist the
     choice with `--reviewer --reviewer-source confirmed`.
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

This stop is machine-readable. `request_rereview.py --json` exits `0` and prints
only JSON to stdout:

```json
{
  "status": "no_safe_trigger",
  "posted": false,
  "reviewer": "<login>",
  "message": "No safe re-review trigger known for `<login>`; pass --review-trigger-mention to enable re-review requests."
}
```

The non-JSON path prints the same message as a `[loop]` block and exits `0`.
Network/API failures still exit non-zero. The agent treats `status:
no_safe_trigger` as a controlled stop to relay exactly, not as a successful
re-review request.

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
- discovery order is based on review-thread comment appearance, not a guessed
  thread-level author.

Additions to `tests/test_fetch_gemini_threads.py`:
- persisted reviewer prevents silent switching when another bot appears later.
- explicit `--reviewer` switch persists the new reviewer.
- `--reset-reviewer` clears the saved reviewer and allows rediscovery.
- `--list-reviewers` prints candidates without mutating state, uses paginated
  discovery data, and marks partial results instead of reporting a false zero.
- JSON output includes `reviewerSelection.confirmation_required` and
  `source: default_unconfirmed` when Gemini is only the compatibility fallback.
- zero candidates on a fresh PR does not force-stop the agent workflow; the
  prompt can choose Gemini-and-wait, manual reviewer, or None.
- omitted `--author` parses as `None`, while `--help` still documents
  `gemini-code-assist` as the effective default.
- Gemini-only PR with no state: back-compat (Gemini selectable/auto-default).
- no implicit Gemini fallback when Gemini did not author review threads.
- unknown bot with review threads appears as a selectable candidate.

Additions to `tests/test_request_rereview.py`:
- missing safe re-review trigger → does not post, exits 0, and returns the
  parsable `status: no_safe_trigger` payload/message.

Run the full existing suite; it must stay green (no reintroduced Gemini
coupling, no breakage of current Gemini behavior).

## Out of scope (v1)

Multi-reviewer aggregation; provider-specific severity parsers; automatic
reviewer switching; GitHub App installation metadata as source of truth;
renaming installed plugin directories or script paths.
