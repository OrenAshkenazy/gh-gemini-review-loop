## Parent PRD

`issues/prd.md`

**Type:** AFK

## What to build

Prefactor enabling fan-out: the env-obligation detector should produce a **list of obligations per env read** instead of exactly one. Today every read yields a single obligation; after this change the detector returns 0..n per read and the orchestrator merges them into the flat obligations list. The rendered card already iterates a flat list, so no render change is expected.

Behavior-preserving on its own (each read still yields one obligation today) — it exists to make the `queue_topic` fan-out an easy change. See the **Fan-out model** under Implementation Decisions in the parent PRD. "Make the change easy, then make the easy change."

## Acceptance criteria

- [ ] The detector returns a list of obligations per env read (not a single obligation)
- [ ] The orchestrator extends the flat obligations list with each read's results
- [ ] A single read is proven capable of yielding more than one obligation (test exercises the multi-emit path)
- [ ] Existing readiness/classification suite stays green (no change to current single-emit outcomes)
- [ ] Pack and readiness objects remain content-free (no infra text / secret values in repr)

## Blocked by

None - can start immediately

## User stories addressed

Reference by number from the parent PRD:

- User story 6
- User story 7
