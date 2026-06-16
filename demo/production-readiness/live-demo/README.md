# Live Demo Assets

Use these seed files to create the real two-repo demo:

| Repo | Purpose |
|---|---|
| `OrenAshkenazy/mergeproof-demo-payments-api` | App repo with trusted `mergeproof.yaml`, capability packs, and a seeded app PR |
| `OrenAshkenazy/mergeproof-demo-platform-infra` | Infra repo with safe fixture-like production paths |

Current live demo URLs:

- App repo: https://github.com/OrenAshkenazy/mergeproof-demo-payments-api
- Infra repo: https://github.com/OrenAshkenazy/mergeproof-demo-platform-infra
- Seeded app PR: https://github.com/OrenAshkenazy/mergeproof-demo-payments-api/pull/1
- Generated infra PR: https://github.com/OrenAshkenazy/mergeproof-demo-platform-infra/pull/1

The app PR should add the files from `app-pr-files/` and use
`app-pr-summary.md` as the PR body or first comment. That summary carries the
CR-loop metrics the readiness phase consumes conceptually: fixed findings,
verification result, re-review cycles, and false positives skipped.

The app repo's `mergeproof.yaml` and `capabilities/` directory can be
regenerated from the infra repo layout:

```bash
SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts

python3 "$SCRIPTS/mergeproof.py" init \
  --repo-root /path/to/mergeproof-demo-platform-infra \
  --repo OrenAshkenazy/mergeproof-demo-payments-api \
  --infra-repo OrenAshkenazy/mergeproof-demo-platform-infra \
  --service payments-api \
  --output /path/to/mergeproof-demo-payments-api/mergeproof.yaml \
  --force
```

`mergeproof init` discovers writable infra paths plus capability packs from the
infra structure. For this fixture, `workers/` paths infer `worker_deployment`;
`secrets/` and Helm `env/` paths infer `secret_wiring`.

After the app PR exists and either a CR-loop terminal record or a CR-loop
metrics table is present in the PR body, run:

```bash
SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts

python3 "$SCRIPTS/mergeproof.py" run \
  --pr https://github.com/OrenAshkenazy/mergeproof-demo-payments-api/pull/1 \
  --publish \
  --stage-infra \
  --create-infra-pr \
  --html-output /tmp/mergeproof-demo-readiness.html
```

Expected result:

- the app PR readiness comment is created or updated in place;
- `worker_deployment` is detected from `app/workers/refund_worker.py`;
- a branch like `mergeproof/worker_deployment-refund_worker` is pushed to the
  infra repo;
- an infra PR is created or reused with Kubernetes, Helm, and Terraform files;
- the app PR readiness comment links to that infra PR;
- `secret_wiring` remains human-gated if the app PR also adds a secret adapter.

Keep `payments-api/` for deterministic offline replay. Use this directory for
the live GitHub demo.
