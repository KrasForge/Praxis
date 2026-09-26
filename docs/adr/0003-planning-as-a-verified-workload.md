# 0003. Planning runs as a verified workload

- **Status:** Accepted
- **Date:** 2026-09-26
- **Supersedes:** none

## Context

Modulo takes requests from people. Praxis runs `ProcessSpec`s and process
graphs. Nothing turns a request such as "fix the flaky test" into processes,
dependencies, budgets and contracts.

The contract matters most. Verification is only as strong as the contract it
checks. A process with an empty contract can reach `State.COMPLETED` without a
check that means anything.

Three rules limit where planning can live:

- The kernel stays harness-independent. It never depends on the result
  semantics of a model, and planning is usually model-driven.
- A spec carries no authority. A submitted spec that contains `capabilities` is
  rejected (TM-1).
- There is no global graph daemon. The host resolves graph readiness.

## Decision

1. **No planner in the kernel, and no new ecosystem service.** A plan is the
   output of an ordinary Praxis process. Any executor can produce it, and an
   agent adapter is the usual one.
2. **A plan is a versioned artifact.** The planning process lists `plan.json`
   as a required output. The file uses the closed schema `praxis.plan` v1:
   - a set of nodes, each a `ProcessSpec`, with no authority;
   - the dependencies between the nodes, with the requirement and the policy of
     each edge;
   - the grants that each node requests, as requests only.
3. **A plan validator checks the structure.** A new validator, `plan`, checks:
   - the schema and the spec of each node;
   - that the graph has no cycle;
   - that every executor is registered;
   - that the node budgets fit inside the budget of the parent;
   - that every node has a non-empty contract;
   - that every requested grant is inside an allowlist of the host.

   A plan that fails these checks cannot reach `State.COMPLETED`.
4. **Running a plan is a separate, audited step.** A verified plan does
   nothing by itself. A caller with the `approve` role materializes it through
   `POST /v1/processes/{id}/plan/materialize`. The host then creates the child
   processes and the stored graph. It issues only the requested grants that its
   configuration allows, and it records who materialized the plan.
5. **The other systems keep their roles.** Modulo shows a plan for review.
   Noesis can supply earlier plans as context. Neither runs one.

## Consequences

- The ecosystem stays at three parts. Planning is a workload that runs on
  Praxis, not a fourth system.
- Plans get the same guarantees as other work: a sandbox, a budget, a verified
  snapshot and an audit trail.
- The validator can enforce the structure of contracts, not their quality. A
  person who reviews the plan before materializing it stays responsible for
  whether the contracts check the right thing.

## Compatibility

- `praxis.plan` v1 and the `plan` validator are new. Unknown fields and unknown
  versions are rejected.
- The materialize route is additive to the v1 API. Graph storage does not
  change.

## Verification

- Validator tests: a cycle, an unregistered executor, a budget over the limit,
  an empty contract, a grant outside the allowlist, and a node that carries
  `capabilities`.
- An end-to-end test: a fixture executor writes a plan; after
  materialization, the graph and the children exist, and only allowed grants
  were issued.
- An authority test: a caller without `approve` cannot materialize, and a plan
  whose process failed cannot be materialized.
