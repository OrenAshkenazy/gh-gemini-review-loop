## Parent PRD

`issues/prd.md`

**Type:** AFK

## What to build

Make `runtime_config` first-class on the demo so a config-precedent env var renders a **matched** obligation end-to-end. The engine already routes a `config` classification to the `runtime_config` capability; the gap is that the demo repo does not declare it, so the path dead-ends. Declare the `runtime_config` capability in the demo `mergeproof.yaml` and ship its capability pack, so a `_URL`-style variable with config precedent in the infra slice resolves to a matched `runtime_config` obligation that appears on the rendered card (not a human gate).

See **Capability vocabulary** and the `CHARGEBACK_PROVIDER_URL -> runtime_config` mapping in the parent PRD.

## Acceptance criteria

- [ ] The demo declares the `runtime_config` capability and a real pack backs it
- [ ] An env var with config precedent in the infra slice routes to a matched `runtime_config` obligation
- [ ] That obligation renders on the readiness card with its app-source evidence
- [ ] A no-precedent var still falls through to `unknown` -> human gate (unchanged)
- [ ] Pack and readiness objects remain content-free
- [ ] Exercised through the injected-runner seam end-to-end

## Blocked by

None - can start immediately

## User stories addressed

Reference by number from the parent PRD:

- User story 2
- User story 8
