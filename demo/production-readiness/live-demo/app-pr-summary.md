## Demo PR: add refund worker

Adds a background worker for refund processing.

### CR loop metrics

| metric | value |
|---|---|
| findings fixed | 5 |
| false positives skipped | 1 |
| verification | passed |
| verification command | pytest |
| re-review | completed |
| cycles used | 2 / 3 |

### Expected MergeProof outcome

MergeProof should detect a `worker_deployment` obligation from
`app/workers/refund_worker.py`, stage generated infra in
`OrenAshkenazy/mergeproof-demo-platform-infra`, create or reuse the infra PR,
and update this app PR with a readiness comment linking to the infra PR.
