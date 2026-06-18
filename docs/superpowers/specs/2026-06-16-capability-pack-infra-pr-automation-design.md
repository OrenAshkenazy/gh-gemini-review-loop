# Capability-Pack Infra-PR Automation — Design

**Date:** 2026-06-16
**Status:** Approved for planning
**Branch context:** builds on `feat/production-aware-pr-readiness` (PR #46, MergeProof readiness)

## Framing (read first)

**Why this is not a linter.** Detection alone is a linter. **Detection + flow
placement + a generated infra change + proof** is the product. A linter tells you
something is wrong; MergeProof shows the missing production obligation *on the
production flow*, generates the change that satisfies it, and proves it — so the
human approves risk, not glue work.

**Demo vs Real Product Boundary.** This PRD optimizes for a **deterministic
VC / design-partner demo**. It intentionally proves the product loop —
obligation → flow placement → generated change → proof → human approval — *before*
adding live GitHub/cloud integration. Everything here runs offline, reproducibly,
with no backend. The path to the real product is defined in **Post-Demo Product
Wedge** below: a GitHub App / check that comments on app PRs and opens linked
infra PRs. The demo proves the loop; the wedge ships it.

## Problem

The MergeProof PR Readiness Card correctly reaches `HUMAN_DECISION_REQUIRED`
when a PR touches production-facing surfaces, but it stops there with two gaps:

1. **No concrete action item for the developer.** The card's "next options"
   are generic ("approve / ask AI to adjust / split"). It does not tell the dev
   *what specific infra change this PR implies, in which repo, who approves it,
   and what they must still provide by hand.*
2. **No path to the infra-repo change.** When an app PR implies a production
   change (a new worker needs a Deployment, a new secret needs wiring), nothing
   generates that change or opens the corresponding PR in the infra repo. The
   pipeline dead-ends.

The `capability_pack.py` module and the `demo/production-readiness/payments-api`
fixtures already seed the intended model: a **Capability Pack** is the approved,
reusable template for one class of infra change. This design completes the
pipeline from "app PR" → "matched obligation" → "concrete action item" →
"staged, one-click infra PR."

## Goals

- Turn each production-facing change into a **specific, actionable** statement
  (what infra change, which repo, which approver, which human gates remain).
- Generate the infra artifacts from approved capability packs and stage a branch
  so the developer can open the infra PR in **one click** from the HTML report.
- Stay **deterministic, zero-dependency, advisory-by-default**, and bound by the
  trusted `mergeproof.yaml` allowlist and the packs' own approval/human gates.

## Non-Goals (this iteration)

- No local action-server / hosted web app (north-star, see Phasing → Phase 3).
- No auto-merge and no auto-approval. Human approval via the pack's
  `approval.required_from` is always required.
- No LLM-based obligation detection. Detection is deterministic rules only.
- No new GitHub identity. Infra writes use the developer's local `gh`.

## Decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Ambition | Full automation is the **north-star architecture**; built in phases. |
| Infra-write identity (now) | Developer's local `gh` pushes the branch; GitHub's own create-PR page is the auth surface for opening the PR. |
| HTML button action (now) | Branch is pushed during the run; the button **deep-links to a prefilled GitHub PR-create page**. |
| Obligation detection | **Deterministic rules** keyed to capability types declared in `mergeproof.yaml`. |

## Architecture

### Pipeline placement

A new stage in `mergeproof run`, between the existing risk overlay and the card
render:

```
... → 19  map changed files → production risks
     → 19b OBLIGATION RESOLUTION            ← new
     → 20  combine evidence + risks + obligations
     → 22  render card (+ action panel)
     → 23  publish PR comment
     → 24  render HTML (+ one-click button)
```

### Components (all stdlib, zero-dependency)

| Unit | Responsibility | Depends on |
|---|---|---|
| `pr_obligations.py` | **Detect.** Deterministic rules map changed files → obligations `{type, inputs, evidence_files}`. Rules are keyed to capability `type`s **declared in `mergeproof.yaml`** — only obligations with an approved path are raised. | changed files, capabilities map |
| `capability_pack.py` *(reuse)* | **Match.** `capabilities_from_config` + `load_pack` resolve each obligation `type` → pack (`generates`, `checks`, `approval`, `human_gate`). Already built and tested. | `mergeproof.yaml`, pack files |
| `generate_infra_change.py` | **Generate.** Render each pack `generates:` entry into infra files via safe `${input}` substitution. Refuse on missing required input; never fill a `human_gate` value. | matched pack, inputs, template files |
| `publish_infra_pr.py` | **Stage.** Create branch in the infra repo, write generated files, commit, push via local `gh`. Return branch ref + prefilled PR-create deep-link. | generated files, `gh` |
| `render_pr_readiness.py` *(extend)* | Render the per-obligation **action panel** in the Markdown card. | `obligations` block |
| `render_demo_ui.py` *(extend)* | Render the action panel in HTML; button = the deep-link (present only when `infra_pr.pushed`). | `obligations` block |

### The three obligation outcomes

1. **`matched`** — pack found, all inputs available, no human gate. Generate
   files, push branch, emit deep-link. Action item: *"Adds a worker → needs
   `worker_deployment` in `acme/platform-infra`, approver `@platform-runtime`.
   [Open infra PR ▸]"*
2. **`human_gated`** — pack found but a required value is human-only (e.g.
   `secret_wiring` needs a secret *value*). Generate the wiring, leave the gated
   value as an explicit unfilled placeholder, record it in `human_gate_pending`.
   Action item names exactly what the human must provide before merge.
3. **`blocked`** — no matching pack. Per the capability-pack contract, an
   obligation with no matching pack is blocked: **no generation, no push.**
   Action item: *"Implies infra X but no approved capability exists — escalate
   to platform."*

## Data shapes

Additive new top-level key in `readiness.json` (nothing existing changes shape):

```json
"obligations": [
  {
    "type": "worker_deployment",
    "outcome": "matched",
    "evidence_files": ["app/workers/refund_worker.py"],
    "inputs": {"worker_name": "refund_worker", "service": "payments-api", "topic": "refunds"},
    "pack": {
      "generates": ["worker_deployment", "helm_worker_values"],
      "checks": ["helm_template", "policy", "naming_convention"],
      "approver": "@platform-runtime",
      "human_gate": null
    },
    "infra_pr": {
      "repo": "acme/platform-infra",
      "branch": "mergeproof/worker_deployment-refund_worker",
      "create_url": "https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?expand=1&title=...&body=...",
      "pushed": true,
      "generated_files": ["envs/prod/payments-api/workers/refund_worker.yaml"]
    },
    "human_gate_pending": []
  }
]
```

`blocked` and `human_gated` outcomes omit `infra_pr.pushed: true` and carry the
reason in the action item; `human_gated` populates `human_gate_pending`.

## Status logic

Two new rules, inserted before the generic human-decision rule:

```
verification failed                          -> VERIFICATION_FAILED
MergeProof config changed in PR              -> CONFIG_CHANGED_REVIEW_REQUIRED
any obligation outcome == blocked            -> HUMAN_DECISION_REQUIRED  (no approved path)   [new]
any obligation human_gate_pending nonempty   -> HUMAN_DECISION_REQUIRED  (value needed)       [new]
any production risk needs a human            -> HUMAN_DECISION_REQUIRED
semantic_risk flagged                        -> HUMAN_DECISION_REQUIRED
fixes applied, not yet re-confirmed          -> PENDING_CONFIRMATION
otherwise                                    -> READY
```

`READY` is reachable only when every obligation is `matched`, generated, and its
infra PR is staged with no pending human gate.

## Generation mechanics

- Each `generates:` entry resolves to a **template file** shipped alongside the
  capability pack within the infra-repo allowlist, e.g.
  `capabilities/templates/worker_deployment.yaml.tmpl`.
- Rendering is **safe `${input}` substitution** via `string.Template.substitute`,
  which *raises* on a missing key (no silent blanks). Inputs come only from the
  deterministic detector. No code execution, no Jinja, no third-party engine.
- **`human_gate` values are never substituted.** The template marks them
  `${HUMAN_GATE:secret value}`; the generator leaves a literal, greppable
  placeholder and records the gate in `human_gate_pending`.

## Branch + deep-link mechanics

- Branch name: `mergeproof/<type>-<primary_input>` (collision-safe; force-updated
  on re-run).
- `publish_infra_pr.py` writes the generated files, commits with a templated
  message, and pushes via local `gh`/git.
- `create_url` is a GitHub **compare URL** with `expand=1` plus URL-encoded
  `title` and `body`. The body embeds the approver `@mention`, a link back to the
  source app PR, the generated-file list, and any pending human gate. Clicking it
  opens GitHub's own create-PR page, prefilled, where the developer is already
  authenticated.

## Safety invariants

- **No pack ⇒ no generation, no push.** Blocked obligations never touch the infra
  repo.
- **`--dry-run` honored.** Stages files to a temp dir, prints the would-be push +
  URL, writes nothing remote.
- **Allowlist-bound.** Every generated path must fall under the trusted
  `mergeproof.yaml` allowlist; a path outside it is a hard error.
- **Idempotent.** Re-running updates the same branch; the PR comment and HTML
  always reflect the latest staged branch.

## UI Scope & MVP Priority

The demo UI is a **static, self-contained HTML report** organized as tabs
(tabs = CSS / tiny-JS toggle; all content pre-rendered, no network, no backend).
To match the deterministic-demo framing, **approve / waive actions and the
readiness pill are deterministic deep-links and rendered state — not live
mutations.** Live mutation is the GitHub-App wedge (Phase 3), not the MVP.

The UI is ambitious (Production Flow, Resolve, diffs, proof, audit trail,
approve/waive, live readiness pill, Capability Packs). To protect the build,
ship in this **priority order** — earlier tabs must be excellent before later
ones get polish:

| Pri | Surface | MVP bar | Notes |
|---|---|---|---|
| 1 | **Readiness tab** | **Excellent** | The verdict + evidence + status pill. The thing a viewer reads first. |
| 2 | **Production Flow** | **Excellent** | Obligations placed on the production flow. **This is half the "wow."** |
| 3 | **Resolve tab** | Shows **generated diffs + proof** | The generated infra change and its proof (checks/render). The other half of the "wow." |
| 4 | **Audit trail** | **Simple** | A plain chronological list. Do not over-build. |
| 5 | **Capability Packs** | **Read-only, lightweight** | Render declared packs (`generates`, `checks`, approver, gates). No editing. |

**Guardrail:** do not let Tab 5 (Capability Packs) or the Audit trail consume
time. The "wow" is **Production Flow + generated proof**, not admin UX. If time is
short, cut polish from 4 and 5 first.

## Phasing

The spec documents the full pipeline; implementation lands in slices.

| Phase | Deliverable | Writes infra repo? | Demo shows |
|---|---|---|---|
| **1 — Detect + advise** | `pr_obligations.py` + pack matching + `obligations` block + action panel (text only, no button) | No | Card names the specific infra change, repo, approver, human gates |
| **2 — Generate + one-click** | `generate_infra_change.py` + `publish_infra_pr.py` + deep-link button in HTML | Yes (branch push via local `gh`) | Click button → prefilled GitHub PR-create page for generated infra branch |
| **3 — Wedge (spec'd, not built)** | **GitHub App / check** that comments on app PRs and opens linked infra PRs (live mutation); hosted view second. See Post-Demo Product Wedge. | Yes (live) | future work only |

**First implementation plan = Phase 1 + Phase 2 together** (Phase 1 alone has no
button to demo; the "now" experience needs both). Phase 3 is recorded here as
future work only.

## Testing

GitHub calls stay behind an injectable client, matching the existing pattern.

- `test_pr_obligations.py` — new worker file → `worker_deployment`; new
  `os.environ[...]` → `secret_wiring`; topic usage → `topic_queue`; change with
  no declared capability → `blocked`; infra-irrelevant change → no obligation.
- `test_generate_infra_change.py` — `${input}` substitution; missing required
  input raises; `human_gate` placeholder left unfilled and recorded; generated
  path outside allowlist is a hard error.
- `test_publish_infra_pr.py` — branch name, commit, compare-URL construction
  (title/body/approver encoding); `--dry-run` writes nothing remote; idempotent
  re-run.
- `test_render_pr_readiness.py` / `test_render_demo_ui.py` — action panel for
  each of `matched` / `human_gated` / `blocked`; button present only when
  `infra_pr.pushed`.
- Status-logic tests — `blocked` and `human_gate_pending` each force
  `HUMAN_DECISION_REQUIRED`.

## Definition of Done

Engineering acceptance (the **Testing** section) plus:

- **Foundation committed.** The currently-untracked seed is brought under version
  control as the first step, since the new modules build on it:
  - `plugins/.../scripts/capability_pack.py` + `tests/test_capability_pack.py`
    (8 tests, already green) committed.
  - `demo/production-readiness/payments-api/` fixtures (`mergeproof.yaml`, the
    three `capabilities/*.yaml` packs, `changed_files.json`, `loop_summary.json`)
    committed.
  - Loose working-tree noise (`err.log`, `prof_out.json`, `wait_err.log`,
    `wait_out.json`, `skills-lock.json`) gitignored or removed — not committed.
- **Phase 1 + Phase 2 shipped** (detect + advise, then generate + one-click), per
  the Phasing table.
- **Full suite green**, including the new `test_pr_obligations.py`,
  `test_generate_infra_change.py`, `test_publish_infra_pr.py`, and the extended
  render tests.
- **Investor acceptance met.** The static tabbed HTML report lets a viewer
  narrate the five-point story unaided (see Investor Demo Acceptance Criteria).
- **Demo reproducible offline** from the `payments-api` fixtures with no network.

## Demo

Uses the now-committed `demo/production-readiness/payments-api` fixtures.
`changed_files.json` adds `app/workers/refund_worker.py` (+ test) and modifies
`app/providers/acme.py`. Against the declared capabilities:

- new worker → **`worker_deployment`** matched → generates worker Deployment +
  Helm values → branch pushed → button **Open infra PR** (approver
  `@platform-runtime`).
- a provider change that reads a new secret → **`secret_wiring`** →
  **`human_gated`**: wiring generated, secret *value* left blank, action item
  names the gate (approver `@platform-secrets`).

End state: an HTML report whose action panel turns "tests passed but
production-facing" into specific, clickable next steps — closing both gaps.

## Investor Demo Acceptance Criteria

Separate from the engineering acceptance (the **Testing** section above). A
VC / design partner should understand, in **90 seconds**, that:

1. **Green PRs still break production.** Tests pass, review is clean — and it is
   still unsafe to merge.
2. **MergeProof sees the missing production obligations** the green checks do not.
3. **MergeProof places them on the production flow** — not a list, a flow.
4. **MergeProof generates the missing production changes** — real infra diffs,
   with proof.
5. **Humans approve risk, not glue work** — the gate is judgment, not authoring
   YAML.

If a viewer cannot narrate these five from the screen unaided, the demo UI has
failed its job regardless of test coverage.

## Post-Demo Product Wedge

The demo proves the loop offline; the commercial path is **GitHub-native**, not a
hosted dashboard:

- The production version starts as a **GitHub App / check** that comments on app
  PRs and **opens linked infra PRs** — the workflow developers already live in.
- The UI in this spec can later become a **hosted view**, but the GitHub-native
  workflow is the wedge; the dashboard is secondary.
- This keeps the commercial path clear: land inside the PR review surface first,
  expand to a hosted experience second. It also maps cleanly onto Phase 3
  (live action-server → GitHub App), so nothing in the demo MVP is throwaway.

## Path from Demo to Real MVP

This spec is the **Demo MVP PRD** — a deterministic, offline proof of the product
loop. To pre-empt the "nice demo, but does it work outside your seeded world?"
objection, the path to a real product is explicit. The Production Obligation and
Capability Pack model is **identical** across both; only the I/O boundary changes.

```text
Demo MVP (this spec):           Real MVP v1 (separate PRD):
- seeded app PR                 - real GitHub app PR as input
- seeded infra repo             - real app repo + real infra repo
- deterministic detectors       - same deterministic detectors
- mock (dry-run) infra PRs      - generated branch/PR pushed to the infra repo
- generated JSON + diffs        - same generation, on real files
- local static UI               - GitHub PR readiness comment (UI optional)
- declared checks as "proof"    - actual validation command execution
```

The same detector, pack model, generator, and `infra_pr` data structure carry
over unchanged — the real MVP swaps fixtures for `gh` reads, dry-run for a live
push, and declared checks for executed ones. Nothing in the demo is throwaway.

### Readiness states: demo vs real product

The **demo keeps the shipped Phase-1 enum** (`VERIFICATION_FAILED`,
`CONFIG_CHANGED_REVIEW_REQUIRED`, `HUMAN_DECISION_REQUIRED`,
`PENDING_CONFIRMATION`, `READY`). The blocked-vs-human-gate distinction a reviewer
might want at the status level is **already surfaced per-obligation** in the action
panel ("Blocked — no approved capability" vs "Needs a human"), so the demo does
not need a wider enum. Because the demo is static, there is **no live
Approve→READY transition** — the card renders a *computed* state; the deep-link
button does not mutate it.

The **Real MVP v1** introduces a richer, deploy-aware state model (to be
formalized in the Product MVP PRD), because an app PR can be mergeable before the
infra change is actually applied:

```text
VERIFICATION_FAILED          # CR loop verification failed
BLOCKED_MISSING_CAPABILITY   # obligation with no approved Capability Pack
HUMAN_GATE_REQUIRED          # a human-only value (e.g. secret value) is unmet
WAITING_FOR_APPROVAL         # infra PR open, awaiting its declared approver
READY_FOR_MERGE              # app PR safe to merge (infra PR exists + approved)
READY_FOR_DEPLOY             # infra actually applied; safe to deploy
```

`READY_FOR_MERGE` vs `READY_FOR_DEPLOY` captures that merge-readiness and
deploy-readiness are different gates. The demo collapses these into `READY` by
design; the distinction is a Real-MVP concern, not a demo one.

### Second artifact

A separate **MergeProof Product MVP PRD** will cover the real-GitHub version (live
PR input, real cross-repo PR creation, executed validation, the deploy-aware state
model, and the GitHub-App wedge). It is intentionally **not** written yet — this
Demo MVP PRD is the current build spec; the Product MVP PRD follows once the demo
has served its VC/design-partner purpose.
