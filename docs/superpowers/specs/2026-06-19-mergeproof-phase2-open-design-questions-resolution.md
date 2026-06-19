# MergeProof Phase 2 — Open Design Questions Resolution

**Date:** 2026-06-19
**Status:** Approved design, pending implementation plan
**Resolves:** the five Open Design Questions in
`2026-06-18-mergeproof-phase2-cross-repo-production-obligations.md`
**Builds on:**

- `2026-06-18-mergeproof-phase2-cross-repo-production-obligations.md`
- `2026-06-14-mergeproof-production-context-pack-design.md`
- Real-world reference: `fiverr/kong_api_gw` (declarative, db-less Kong gateway)

## Purpose

The Phase 2 spec left five Open Design Questions. They are not five
independent calls — they collapse into three decisions:

| Phase 2 question | Decision |
|---|---|
| Q1 `CHARGEBACK_QUEUE_NAME` → which capability? | **Decision 1** |
| Q2 `CHARGEBACK_PROVIDER_URL` secret vs config default? | **Decision 1** |
| Q3 route support require registration before gateway generation? | **Decision 2** |
| Q4 ingest external review findings as advisory annotations? | **Decision 3** |
| Q5 generated architecture summaries as Layer 1 hints? | **Decision 3** |

One principle runs through all three: **MergeProof stays deterministic and
evidence-cited. The infra repo is the oracle. No probabilistic input is ever an
authority for a verdict or for infra generation.**

## Governing Invariant (cross-cutting)

> Every `supported` / `missing` / `unknown` verdict, every sensitivity
> classification, and every staged infra change traces to cited allowlisted IaC
> evidence at an immutable SHA. MergeProof must produce byte-for-byte identical
> obligations, classifications, and infra PRs whether or not any AI reviewer ran.

Turn Gemini (or any AI reviewer) off entirely and nothing about the generated
infra changes. This is the product moat: detection alone is commodity;
deterministic normalization, evidence matching, capability-gated generation, and
proof are not.

## Decision 1 — Runtime config obligations and infra-precedent classification

Resolves Q1 and Q2. They share one underlying problem: how an env-var obligation
is classified and routed to a capability.

### Two orthogonal axes

The Phase 2 framing conflated two independent questions:

- **Axis A — which artifact wires the value in:** ConfigMap / Helm value
  (non-sensitive) vs ExternalSecret / Secret (sensitive).
- **Axis B — does the name also imply a resource must exist:** an env var that
  names a queue is a `queue_topic` obligation only if a *second* signal fires
  (a producer/consumer call, a queue-client constructor). The env read on its
  own is only an injection obligation.

A single env var can therefore raise two independent obligations. Do not force
one pack to own both.

### New capability: `runtime_config`

Introduce `runtime_config` (ConfigMap / Helm value injection, **no** human gate)
as a capability distinct from `secret_wiring` (ExternalSecret, human-gated
value). They differ in generated artifact and in whether a human-gated value
exists. The router between them is the infra-precedent matcher below — never a
hardcoded name list.

### Q1 — `CHARGEBACK_QUEUE_NAME`

- The env read is a `runtime_config` obligation (config injection), **not**
  `secret_wiring`.
- It becomes a separate `queue_topic` obligation **only if** the infra repo has
  a queue-provisioning pattern (`terraform/.../queues/*.tf`, an SQS/SNS module)
  and this queue is absent from it.
- If the infra has **no** queue-provisioning pattern at all → the resource
  question is `unknown`, never `missing`.

### Q2 — sensitivity is read from infra precedent, not guessed

The infra repo already encodes how this service wires its env vars. Sensitivity
is an evidence-matching problem, not a heuristic.

**Precedent scope (decided): workload-first, then repo-wide fallback, then
`unknown`.**

1. **Workload-scoped match.** Inspect how *this service's* existing env vars are
   wired. Peers under ExternalSecret define the secret set; peers in ConfigMap /
   Helm values define the config set. Classify the new var by which mechanism its
   shape-peers use. Cite the evidence file.
2. **Repo-wide fallback.** If the service has no comparable precedent (thin / new
   infra), widen the search to the rest of the infra repo. A sibling service's
   wiring of the same var shape is still cited evidence — provenance simply points
   at the sibling ("payments-api had none; 9 services wire `*_URL` as config").
3. **`unknown` → human gate.** Only when *nothing* in the whole infra repo
   resembles the var does classification fail loud to `missing_human_gated`.

A global name-pattern table (`KEY|TOKEN|SECRET|PASSWORD|CRED` → secret;
`URL|HOST|NAME|PORT|ENDPOINT` → config) survives **only** as an advisory
suggestion rendered *inside* the human gate to speed the human's pick. It is
never the deciding authority.

`CHARGEBACK_PROVIDER_URL` therefore classifies as config **because** a cited peer
(`*_PROVIDER_URL` in `helm/payments-api/values.yaml`, or a sibling service on
fallback) is a ConfigMap value and no `*_URL` var is wired through ExternalSecret
— not because a name pattern said so.

### Classification output

Every classification verdict ships its evidence, same bar as a support verdict:

```json
{
  "env": "CHARGEBACK_PROVIDER_URL",
  "classification": "config",
  "capability": "runtime_config",
  "precedent_scope": "repo_wide",
  "evidence_files": ["helm/orders-api/values.yaml"],
  "reason": "SHIPPING_PROVIDER_URL (a *_PROVIDER_URL peer) is a ConfigMap value; no *_URL var is wired via ExternalSecret in this infra repo"
}
```

## Decision 2 — Route exposure via structural gateway-file matching

Resolves Q3. Grounded in the real `fiverr/kong_api_gw` declarative config.

### What the real gateway showed

- Exposure is **structurally explicit in one declarative file**
  (`conf/kong.yaml`). A route exists iff a `services[].routes[]` block with
  `hosts` + `paths` was written. Nothing is implicitly exposed. That file fits
  MergeProof's trust model exactly: allowlistable, resolvable to a SHA.
- **"Public" is a citable fact, not an inference.** The `hosts` value is the
  discriminator: `service.fiverr.com` / `*.fiverr.com` = prod-public;
  `*.dev.fiverr.com` = dev. Paths are explicit strings / regex.
- **Broad routes are common.** A service with `paths: ["/"]`, `strip_path: false`
  exposes its entire surface; new app subpaths under it need **no** gateway change.
- **Real proof exists for free:** `kong config parse | lint | validate` plus a
  dry run. Production-grade proof, not demo-local.

This dissolves the earlier "explicit app marker (A) vs infer-from-path-precedent
(B)" fork. Exposure is never inferred from path shape; it is a structural match
against the gateway file, the same deterministic bar as every other verdict.

### Three deterministic steps, in order

1. **Already-covered check (the common case).** Does the service already have a
   route whose `paths` covers the new path (a `paths: ["/"]` prefix or matching
   pattern) under a prod host? → `supported`, cited to that block. **No
   generation.** Most new app routes land here.
2. **Exact-match check.** Is there a `routes[]` entry for this exact path? →
   `supported`, cited. Absent and not covered → a genuine *new exposure*.
3. **New-exposure gate.** Originating a new `hosts:` binding (e.g.
   `chargebacks.fiverr.com`) is the high-blast-radius act. MergeProof **never
   auto-stages it from inference.** It requires an explicit app-side `public:
   true` declaration (route registry export or `mergeproof.yaml` obligation),
   classifies `missing_human_gated`, routes to the security approver, and stages
   the `routes[]` diff only behind that gate.

Literal Q3 answer: **yes, require explicit intent before generating — but the bar
is a structural match against the declarative gateway file, with new host
bindings always human-gated.** Path-shape precedent inference is dropped entirely.

### Proof

A staged gateway change is proved by rendering the file and running
`kong config validate` (and `parse` / `lint`) on it. This is real proof; the
report labels it as such, not as demo-local.

## Decision 3 — Advisory layer deferred; engine stays AI-free

Resolves Q4 and Q5. Both concern the advisory layer.

### Two distinct AI roles — separate them

AI shows up in two unrelated roles. Conflating them is the mistake to avoid.

| Role | Allowed? | Why |
|---|---|---|
| **Authority over gaps / generation** — AI decides a production gap exists, flips a verdict, or triggers an infra change. | **Never.** | Infra config changes must not be coupled to AI review. The Governing Invariant is the test: identical infra PRs with the reviewer off. |
| **Cross-repo structure discovery** — AI helps *locate and map* a customer's app ↔ infra relationship when it does not fit the clean declared allowlist. | **Permitted when justified**, output normalized first. | Real customers vary: mono-vs-multi-repo, different naming conventions, prod living in non-obvious places. Discovery is hard; deciding is not AI's job. |

The boundary in one line: **AI may help find *where to look*; it may never decide
*what is true* or *what to change*.** Any AI-proposed mapping, candidate file, or
repo relationship is advisory input that must be normalized into cited,
allowlisted, deterministic evidence at an immutable SHA before it can affect a
verdict or a generated change. The deterministic engine remains the sole
authority; AI only widens discovery for heterogeneous customer layouts.

### Position

**Do not build advisory ingestion as a pipeline component in this phase.** An AI
reviewer is not a required input and not a trusted one for finding production
gaps. The Governing Invariant above is the test: identical infra PRs with the
reviewer off. The permitted discovery role above is a *justified-use future
option* for heterogeneous customer repo structures — not built now, and even when
built it never becomes an authority.

- **Q4 — deferred.** Advisory ingestion of review findings is demoted to an
  optional, off-by-default, observational sidecar that lives entirely outside the
  obligation → generation path. If ever built, its sole permitted output is an
  offline *detector-gap log* ("an advisory tool mentioned X; no deterministic
  detector covers it") for a human to decide whether to build a new detector. It
  never annotates a verdict, never feeds generation. For this phase it is a
  documented future slot, nothing more.
- **Q5 — deferred.** A reserved optional `advisory_context` block in
  `mergeproof.yaml` (read from the trusted base ref, separate from
  `architecture_sources`) may later *suggest* candidate related files / repos to
  consider. It can **never** expand the allowlist or authorize a fetch; the
  allowlist stays the only thing that authorizes anything. Build later, if ever,
  only after a deterministic detector-gap corpus exists.

### Why deferred, not killed

The coverage-gap idea has long-term value: it is the labeled corpus the Phase 2
spec says is a precondition for any future ML recall layer. But it is a flywheel
for *improving deterministic detectors offline*, never a runtime authority.
Keeping it out of the critical path now preserves the AI-free engine guarantee.

## Implementation Impact

Maps onto existing scripts in
`plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/`:

| Area | Change |
|---|---|
| `pr_obligations.py` | Replace path-regex detectors with content-aware env/secret detector; emit `runtime_config` obligations; raise `queue_topic` only on a second resource signal. |
| New evidence matcher | Infra-precedent sensitivity classifier (workload-first, repo-wide fallback, `unknown` → gate); emits cited classification objects. |
| New evidence matcher | Gateway-file structural route matcher (already-covered → exact-match → new-exposure gate). |
| `capability_pack.py` | Add `runtime_config` pack distinct from `secret_wiring`; gateway pack stays human-gated for new host bindings. |
| Proof layer | Add `kong config validate` (and `parse` / `lint`) for staged gateway changes. |
| Advisory | No component built. Reserve `advisory_context` schema slot, documented as deferred. |

## Updated Open-Question Status

| Phase 2 question | Resolution |
|---|---|
| Q1 | `runtime_config` for the env read; `queue_topic` only on a second resource signal, else `unknown`. |
| Q2 | Infra-precedent classifier, workload-first → repo-wide → `unknown` gate; no silent default. |
| Q3 | Structural gateway-file match; broad route ⇒ `supported`; new host binding ⇒ human-gated, never inferred. |
| Q4 | Deferred. Optional off-by-default observational sidecar at most; not in the critical path this phase. |
| Q5 | Deferred. Reserved `advisory_context` slot; never authoritative, never expands the allowlist. |

## Out of Scope

- Any AI authority over production gaps, verdicts, or infra generation
  (permanent — not merely deferred).
- AI-assisted cross-repo structure discovery (a justified-use future option for
  heterogeneous customer layouts; not built this phase, and always normalized to
  cited deterministic evidence when built).
- Path-shape inference for route exposure.
- Building the advisory sidecar or `advisory_context` ingestion now.
- ML recall layer (precondition: a deterministic detector-gap corpus, not yet
  collected).
