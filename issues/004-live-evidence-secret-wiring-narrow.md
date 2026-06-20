## Parent PRD

`issues/prd.md`

**Type:** AFK

## What to build

Live acceptance evidence for the env-driven capability routing, plus a guard that `secret_wiring` stays narrow. Arrange the demo infra precedent and demo capabilities so the matched routes fire, and put a demo PR up carrying three variables that exercise all three env-driven capabilities:

- `CHARGEBACK_PROVIDER_URL` -> `runtime_config`
- `CHARGEBACK_QUEUE_NAME` -> `runtime_config` + `queue_topic` (queue resource missing)
- `CHARGEBACK_PROVIDER_TOKEN` -> `secret_wiring`

Do one real `run_readiness` against that PR and confirm the rendered card matches the offline fixtures. Also lock in that `secret_wiring` is reserved for genuine secret precedent: a `_TOKEN` / `_API_KEY` / `_SECRET` / `_PASSWORD` variable routes there, and a plain config variable never does.

See **Target mappings** and the **Live acceptance evidence** testing decision in the parent PRD.

## Acceptance criteria

- [ ] Demo infra precedent + capabilities arranged so `runtime_config`, `queue_topic`, and `secret_wiring` all fire on the demo PR
- [ ] Demo PR carries `CHARGEBACK_PROVIDER_URL`, `CHARGEBACK_QUEUE_NAME`, `CHARGEBACK_PROVIDER_TOKEN`
- [ ] One live `run_readiness` produces: `runtime_config` for the URL var; `runtime_config` + `queue_topic` for the queue var; `secret_wiring` for the token var
- [ ] A `_TOKEN`-style var routes to `secret_wiring`; a config var never routes to `secret_wiring` (asserted in an offline fixture)
- [ ] No infra slice or secret values leak into the rendered pack or readiness object
- [ ] Live run outcome recorded as evidence

## Blocked by

- Blocked by `issues/001-fanout-detector-returns-list.md`
- Blocked by `issues/002-runtime-config-first-class-demo.md`
- Blocked by `issues/003-queue-topic-resource-absence-gate.md`

## User stories addressed

Reference by number from the parent PRD:

- User story 3
- User story 24

---

## Progress note (2026-06-20, AFK loop)

**Offline slice DONE; live-run core deferred (HITL).**

Done autonomously:
- `secret_wiring` narrowness guard: `tests/test_secret_wiring_narrow.py` asserts
  a `_TOKEN` var with genuine secret precedent → `secret_wiring`; a config `_URL`
  var → `runtime_config` and never `secret_wiring`; a `_TOKEN`-*named* var with no
  secret precedent stays `unknown` → `env_classification` (name pattern advisory
  only, never routes). 806 tests green.
- The demo now declares all three env-driven capabilities (`runtime_config`,
  `secret_wiring`, `queue_topic`) in `demo/.../payments-api/fixtures/mergeproof.yaml`,
  and the offline e2e fixtures (`test_runtime_config_demo.py`,
  `test_queue_topic_fanout.py`) prove the matched routes + fan-out through the
  injected-runner seam.

Remaining (needs a human / live GitHub — not autonomously runnable):
- Put up a real demo PR on `mergeproof-demo-payments-api` carrying
  `CHARGEBACK_PROVIDER_URL`, `CHARGEBACK_QUEUE_NAME`, `CHARGEBACK_PROVIDER_TOKEN`,
  with the demo infra precedent arranged (config peer for `_URL`/`_NAME`, secret
  peer for `_TOKEN`, no provisioned queue) so all three routes fire live.
- One real `run_readiness` against that PR; confirm the card matches the offline
  fixtures; record the live outcome as acceptance evidence.

User decision (this session): do the offline slice only; leave the live run.
