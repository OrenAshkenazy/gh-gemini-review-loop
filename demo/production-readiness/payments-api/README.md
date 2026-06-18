# payments-api — Offline deterministic replay

An offline replay of the MergeProof capability-pack flow: a green app PR that
still introduces hidden **production obligations**, placed on a Production Flow,
with generated infra changes, proof, and a one-click "Open infra PR" — while a
human keeps the secret-value gate.

Everything runs locally with **no network, no GitHub, no cloud**. The infra repo
(`acme/platform-infra`) is fictional, so infra staging runs in **dry-run**: real
generated file contents and a constructed PR-create deep-link, no push.

## Run it

From the repo root:

```bash
python3 demo/production-readiness/payments-api/run_demo.py
open demo/production-readiness/payments-api/pr_readiness_report.html   # macOS; or just open the file in a browser
```

Expected console output:

```text
status: HUMAN_DECISION_REQUIRED
  obligation: worker_deployment -> matched
  obligation: secret_wiring -> human_gated
```

`run_demo.py` is idempotent and writes ignored local artifacts
(`readiness.json`, `pr_readiness_report.html`) next to the script, so the demo can
be replayed without committing generated output.

## The 90-second story (what the report shows)

1. **Green PR still unsafe.** The CR loop passed (5 fixed, verification passed),
   yet the verdict is **`HUMAN_DECISION_REQUIRED`**.
2. **Hidden obligations detected.** The PR adds `app/workers/refund_worker.py`
   and `app/secrets/stripe_webhook.py` → a `worker_deployment` and a
   `secret_wiring` obligation.
3. **Placed on the Production Flow.** The **Production Flow** tab shows each
   obligation on the service's architecture, with its approver.
4. **Generated change + proof + one-click.** The **Resolve** tab shows the
   generated infra diff (e.g. `kind: Deployment` for the worker), the declared
   checks as proof, and an **Open infra PR ▸** button (a prefilled GitHub
   compare deep-link).
5. **Human owns the risk, not the glue.** `secret_wiring` is **human_gated** —
   MergeProof generates the wiring but the **secret value** stays a human gate
   (`secret value provisioning`), so a person approves risk, not YAML.

## The five tabs

| Tab | Shows |
|---|---|
| Readiness | The verdict, merge evidence, architecture context, risks |
| Production Flow | Obligations placed on the architecture flow (the hero screen) |
| Resolve | Generated diff + declared-checks proof + the one-click infra-PR button |
| Audit | A simple chronological list of loop + obligation events |
| Capability Packs | Read-only view of the declared packs (generates / checks / approver / gate) |

The HTML is a single self-contained file: embedded CSS, **no JavaScript**, no
external network assets.

## What's under the hood

`run_demo.py` chains the production scripts (in
`plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/`):

```text
load_capabilities_and_packs + detect_obligations   (pr_obligations.py)
  -> stage_obligations (dry-run)                    (stage_obligations.py
                                                     -> generate_infra_change.py
                                                     -> publish_infra_pr.py)
  -> build_readiness                                (render_pr_readiness.py)
  -> render_html (5-tab report)                     (render_demo_ui.py)
```

## Fixtures

| File | Role |
|---|---|
| `fixtures/mergeproof.yaml` | Trusted config: service + declared `capabilities` (worker/secret/topic) |
| `fixtures/capabilities/*.yaml` | The Capability Packs (inputs, generates, checks, approver, human_gate, template_map) |
| `fixtures/capabilities/templates/*.tmpl` | The infra templates each pack renders |
| `fixtures/changed_files.json` | The seeded app PR's changed files |
| `fixtures/loop_summary.json` | The CR loop's terminal record (green) |

This is the deterministic replay path. The live demo path uses the external
`OrenAshkenazy/mergeproof-demo-payments-api` and
`OrenAshkenazy/mergeproof-demo-platform-infra` repositories with
`mergeproof run --pr <APP_PR> --publish --stage-infra --create-infra-pr`.
