<!-- mergeproof-pr-readiness -->
## MergeProof PR Readiness

**Status:** HUMAN DECISION REQUIRED  
**Reason:** AI review loop completed and tests passed, but this PR touches production-facing surfaces.

PR: https://github.com/OrenAshkenazy/familia-ai/pull/106

### Merge evidence

| Signal | Result |
|---|---|
| AI findings fixed | 6 |
| False positives skipped | 1 |
| Verification | `uv run pytest` passed |
| Re-review | Gemini completed |
| Cycles used | 2/3 |

### Production architecture context

| Field | Value |
|---|---|
| Service | `familia-ai` |
| Runtime | Kubernetes |
| Exposure | Public |
| Ingress | Ingress → service |
| Data | PostgreSQL, Redis |
| Async | REDIS `arq` |
| Owner | unknown |
| Production context | 5 files from `OrenAshkenazy/familia-ai-infra@infrash` |

### Production risks

| Severity | Surface | Evidence |
|---|---|---|
| High | Public API | `backend/app/routers/scraper_connectors.py` — PR touches API route/handler code in a public-facing service |
| Medium | Async processing | `backend/app/jobs/worker.py` — PR changes worker/consumer code and the service uses queues |

### Human decision required

Tests passed and AI review findings were fixed, but this PR changes code mapped to production-facing behavior.

Review before merge:
1. API behavior, contract, and error handling
2. worker retry and duplicate-processing behavior
3. whether sensitive credential/banking data could leak in connector or worker logs

### Recommended next options

1. Approve the production risk and merge
2. Ask AI to adjust the implementation
3. Split risky behavior into a follow-up PR
