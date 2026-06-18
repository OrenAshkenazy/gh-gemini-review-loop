# MergeProof — Production Context Pack & PR Readiness (MVP)

**Date:** 2026-06-14
**Status:** Approved design, pending implementation plan
**Builds on:** the single-repo readiness MVP already on branch
`feat/production-aware-pr-readiness` (architecture_context, pr_architecture_risk,
render_pr_readiness, publish_pr_readiness, render_demo_ui).

---

## 1. Problem & positioning

The existing Gemini code-review (CR) loop answers:

> Did the AI address the review feedback and pass verification?

MergeProof readiness adds the layer on top:

> Given that result, is this PR safe to merge **in production**?

Production reality usually does not live in the app repo. It lives in an infra
repo. `mergeproof.yaml` (in the app repo) is the bridge that declares where the
service's real production infrastructure is described. MergeProof fetches a
bounded, allowlisted slice of that infra, extracts normalized facts, and assembles
a **Production Context Pack** — a stable "service nutrition label". The PR's
changed files are then overlaid as a **risk overlay** to produce a **Readiness
Card**.

The readiness phase is the **final merge-readiness layer chained onto the existing
CR loop**, not a separate workflow.

## 2. Approved decisions

- **B — app declares, token-governed.** `mergeproof.yaml` in the app repo lists
  the infra repo, ref, and a glob allowlist. Cross-repo read access rides on the
  existing `gh` token's permissions. No infra-side opt-in file.
- **1 — always assemble the full declared pack.** The pack is every fact
  extracted from the allowlisted paths, independent of the diff. Changed files
  drive only the risk overlay, never which infra files are fetched. (Diff-scoped
  "blast radius" mode is explicit future work, not now.)
- **C — CLI core now, GitHub Action later.** Build testable CLI components and a
  phase orchestrator now. The GitHub Action is a later thin wrapper around the
  same CLI; not built this round.

"Full pack" means **full pack of the allowlisted paths**, never a scan of the
entire infra repo.

## 3. End-to-end flow (entry point)

The product entry point is the **existing CR loop**, extended with a readiness
phase. Conceptually `mergeproof run --pr <URL>`; in current script terms, the CR
loop reaches its terminal summary and then automatically runs readiness.

```
Gemini review comments
→ Claude fixes actionable findings
→ verification
→ Gemini re-review
→ CR loop terminal summary (loop_summary.json)
→ [readiness phase] build Production Context Pack
→ map changed files to production risks
→ render MergeProof PR Readiness Card
→ publish / update PR comment
```

Internal sequence of the readiness phase:

```
run_review_loop
→ write loop_summary.json
→ build_context_pack.py        → production_context_pack.json
→ pr_architecture_risk.py      → production_risks.json
→ render_pr_readiness.py       → readiness.json / readiness.md
→ publish_pr_readiness.py      → PR comment
```

### Gating

- If `mergeproof.yaml` (or `mergeproof.json`) **exists** on the trusted ref →
  readiness runs by default.
- If it is **missing** → the CR loop completes normally and readiness is skipped
  **without failing the loop**, printing exactly:

  ```
  [mergeproof] readiness skipped
  Reason: mergeproof.yaml not found
  ```

- A `--readiness` flag may force the phase on; absence-of-config still yields the
  skip message.

### Verification-failed behavior

If verification failed, readiness **still renders a card**, but the status is
forced to `VERIFICATION_FAILED` and production-risk analysis is secondary (shown,
but not the headline). Status precedence:

```
VERIFICATION_FAILED
> CONFIG_CHANGED_REVIEW_REQUIRED
> HUMAN_DECISION_REQUIRED
> PENDING_CONFIRMATION
> READY
```

## 4. Trust model (mandatory)

**`mergeproof.yaml` is read from the trusted base ref, never PR head by default.**

A PR can modify `mergeproof.yaml` to widen the infra paths MergeProof reads.
Therefore:

```
PR changed files : from the PR head
mergeproof config: from the trusted base branch (base.sha of the PR)
infra files      : only the allowlisted paths in the trusted config
```

- Base ref/sha is resolved from the PR (`gh api repos/{app}/pulls/{n}` → `base.sha`).
- Config is fetched at that immutable base sha.
- If `mergeproof.yaml` (or `.json`) appears in the PR's changed files, MergeProof
  reports `CONFIG_CHANGED_REVIEW_REQUIRED`, uses the **base-branch** config for the
  run, and surfaces:

  ```
  MergeProof config changed in this PR.
  Using base-branch config for this run.
  Review config changes before they affect production context resolution.
  ```

- Optional explicit override `--trust-pr-config` (reads config from PR head) may
  exist, but **is never the default**.

## 5. Config format & parser (decision: vendored subset + JSON)

The plugin stays **zero-dependency**. No PyYAML.

- `mergeproof.json` is the canonical machine format.
- `mergeproof.yaml` is a human-friendly **strict subset**, parsed by a small
  vendored loader.

**Allowed YAML subset:** block maps, block lists, strings, integers, booleans,
comments, simple quoted/unquoted scalars.

**Rejected (hard error):** anchors, aliases, merge keys, inline/flow maps,
multiline strings, complex YAML typing, duplicate keys.

On anything outside the subset:

```
Unsupported mergeproof.yaml syntax. Use the documented subset or mergeproof.json.
```

Duplicate keys are rejected explicitly (not last-wins).

### Schema

```yaml
version: 1
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main                      # branch, tag, or SHA
    allow:
      - envs/prod/aegislocal-api/**
      - modules/kong/**
      - modules/sqs/**
      - modules/redis/**
limits:                            # optional; defaults applied if absent
  max_files: 200
  max_file_bytes: 262144           # 256 KiB
```

## 6. Fetch & extraction

1. **Resolve ref once per source** to an immutable commit SHA
   (`gh api repos/{infra}/commits/{ref}` → sha). All blob fetches use the resolved
   SHA. Record `resolved_sha` in provenance.
2. **List** the tree once: `gh api repos/{infra}/git/trees/{sha}?recursive=1`.
   - If the API returns `truncated: true`, proceed with the partial listing and
     record a clear warning in the safety report (`tree_truncated: true`); do not
     silently pretend it is complete.
3. **Filter** listed paths by the allow globs (`fnmatch` with `**` support).
   Enforce `max_files` — when the allowlist matches more than `max_files`, stop
   safely, fetch up to the cap, and record the overflow in the skip report.
4. **Fetch blobs** for matched files; per file enforce `max_file_bytes`, skip
   binaries (null-byte detection / known binary extensions). Each skip is recorded
   with a reason (`too_large`, `binary`, `over_max_files`, `not_allowlisted`).
5. **Extract facts** by reusing the existing `architecture_context` extractors run
   over the fetched in-memory files (see §8 refactor).
6. An **inaccessible infra repo / source** yields a partial pack (that source
   recorded as failed in the safety report) — never a crash.

GitHub access is isolated behind an injectable runner so all of the above is
unit-testable offline.

## 7. Artifacts (kept separate)

```
production_context_pack.json   service nutrition label (stable, cacheable)
production_risks.json          risk overlay from changed files
readiness.json / readiness.md  merge decision surface
```

The pack **must not** contain PR changed files. Mental model:

```
Production Context Pack = service nutrition label
Changed PR files        = risk overlay
Readiness Card          = merge decision surface
```

### Production Context Pack shape

```json
{
  "service": "aegislocal-api",
  "facts": { "...normalized architecture_context shape: runtime, exposure,
              ingress, datastores, queues, secrets_or_env (NAMES ONLY), ..." },
  "provenance": {
    "sources": [
      {"repo":"acme/infra","ref":"main","resolved_sha":"abc123",
       "files":["envs/prod/aegislocal-api/deployment.yaml", "..."]}
    ],
    "fetched_at": "2026-06-14T00:00:00Z",
    "file_count": 4
  },
  "safety": {
    "limits": {"max_files":200,"max_file_bytes":262144},
    "skipped": [{"path":"...","reason":"binary|too_large|over_max_files|not_allowlisted"}],
    "tree_truncated": false,
    "failed_sources": [],
    "secrets_redacted": true
  }
}
```

**Hard boundary:** normalized facts + evidence paths only. The pack never includes
raw Terraform/YAML file contents, and never includes secret **values** — the
extractor emits environment/secret **names** only.

## 8. Code layout

**New scripts** (in the existing
`plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/` dir):

| Script | Single responsibility |
|---|---|
| `mergeproof_config.py` | Parse `mergeproof.json` / strict-subset `mergeproof.yaml`; validate schema; reject unsupported syntax & duplicate keys. |
| `resolve_mergeproof.py` | Fetch config from the **trusted base ref**; detect PR-modified config (`CONFIG_CHANGED_REVIEW_REQUIRED`); resolve `architecture_sources` to `repo + resolved_sha + allow`. |
| `fetch_infra_files.py` | List tree once, glob-filter, enforce limits, skip binaries, return fetched files + safety report. GitHub calls behind injectable runner. |
| `build_context_pack.py` | Orchestrate resolve → fetch → extract; **emit `production_context_pack.json` only** (no markdown). |
| `mergeproof_readiness.py` | The **phase orchestrator**: implements gating (skip / config-changed / verification-failed), then chains build_context_pack → pr_architecture_risk → render_pr_readiness → publish_pr_readiness. This is what the CR loop's terminal phase calls and what integration tests drive. |

**Reused / lightly refactored:**

- `architecture_context.py` — split `scan(dir)` into `_collect_files(dir)` +
  `extract_facts(mapping)`; the fetched-files path feeds `extract_facts`. `scan()`
  keeps working; existing tests stay green.
- `pr_architecture_risk.py` — `assess(facts, changed_files)` unchanged; receives
  `pack["facts"]`. Continues to emit `production_risks.json`.
- `render_pr_readiness.py` — unchanged contract; add a provenance line in the
  architecture-context section ("Production context: N files from
  `acme/infra@<sha>`") and surface `CONFIG_CHANGED_REVIEW_REQUIRED` when set.
- `publish_pr_readiness.py` — unchanged.
- `render_demo_ui.py` — unchanged; consumes `readiness.json`.

`build_context_pack.py` is a component, **not** the product entry point.

## 9. Skill integration

The review-loop SKILL.md terminal phase invokes `mergeproof_readiness.py` after
the terminal summary is written. The skill provides prose/UX; the gating and
chaining logic live in `mergeproof_readiness.py` so they are testable. The richer
`mergeproof run` UX and the GitHub Action are later wrappers around this same
orchestrator.

## 10. Testing

Reuse the existing zero-network, injected-runner pattern. New/added tests:

**Config parser**
- duplicate YAML keys are rejected
- unsupported YAML syntax is rejected with the documented message
- `mergeproof.json` and strict `mergeproof.yaml` parse to the same structure

**Trust model**
- `mergeproof.yaml` changed in the PR uses base config by default and reports
  `CONFIG_CHANGED_REVIEW_REQUIRED`
- `--trust-pr-config` explicitly uses PR-head config

**Fetch**
- broad allow glob hits `max_files` and stops safely (overflow recorded)
- recursive tree truncation returns partial context with a clear warning
- source ref is resolved once and all file fetches use the resolved SHA
- inaccessible infra repo returns partial context, not a crash
- `max_file_bytes` and binary files are skipped with recorded reasons

**Pack integrity**
- pack never includes raw infra file contents
- pack never includes secret values (names only)
- pack output is deterministic

**Phase orchestration**
- CR loop terminal summary triggers the readiness phase when `mergeproof.yaml` exists
- missing `mergeproof.yaml` skips readiness without failing the CR loop (exact message)
- verification failed still renders a readiness card with status `VERIFICATION_FAILED`

**Offline fixtures**
- a mock infra file set + mock `mergeproof.yaml` drive the full chain without network

Run: `/opt/homebrew/bin/pytest -q`.

## 11. Out of scope (MVP)

- Diff-scoped "blast radius" pack (Option 2).
- GitHub Action automation (later thin wrapper).
- Infra-side allowlist / two-sided handshake (Option A).
- Central catalog resolution (Option C from source-model discussion).
- Pack caching (artifact is shaped to be cacheable; caching itself is later).

## 12. Open items for plan stage

- Exact precedence handling and rendering of `CONFIG_CHANGED_REVIEW_REQUIRED`
  inside `render_pr_readiness` (banner + status bump) — confirm during planning.
- Whether `mergeproof_readiness.py` reads `loop_summary.json` from a path argument
  or resolves it from loop state — align with existing `--cycle-summary` output.
