## Parent PRD

`issues/prd.md`

**Type:** AFK

## What to build

`queue_topic` as a fan-out consumer. A worker-scope, queue-ish env variable (suffix indicating a queue, e.g. `_QUEUE` / `_NAME` in a queue context) always emits a `runtime_config` obligation, and **additionally** a `queue_topic` obligation **only when a resource-absence check finds no provisioned queue resource for the workload** in the infra slice. If a matching queue/topic resource already exists, only `runtime_config` is emitted.

Ship the `queue_topic` capability pack and the structural resource-absence check. The check scans the in-process infra slice for a provisioned queue/topic bound to the workload (e.g. a Terraform `aws_sqs_queue` / `aws_sns_topic` or a declared queue in Helm values). It is driven by **structural absence**, never by the variable's value, and records the infra **paths** it inspected as evidence — never infra content.

See **Resource-absence check (queue_topic gate)** in the parent PRD.

## Acceptance criteria

- [ ] A queue-ish var with **no** provisioned queue in the infra slice yields **two** obligations: `runtime_config` + `queue_topic`
- [ ] The **same** var with a provisioned queue resource present yields **one**: `runtime_config` only
- [ ] The `queue_topic` pack exists and backs the emitted obligation
- [ ] The resource-absence check cites the infra paths it inspected, not infra content or env values
- [ ] Pack and readiness objects remain content-free (no queue resource text in repr)
- [ ] Status escalates to HUMAN_DECISION_REQUIRED when a resulting obligation is human-gated
- [ ] Exercised through the injected-runner seam end-to-end (both branches)

## Blocked by

- Blocked by `issues/001-fanout-detector-returns-list.md`
- Blocked by `issues/002-runtime-config-first-class-demo.md`

## User stories addressed

Reference by number from the parent PRD:

- User story 4
- User story 5
- User story 6
- User story 14
