# 0001. Effect adapters and the host effect pipeline

- **Status:** Proposed
- **Date:** 2026-09-26
- **Supersedes:** none

## Context

The kernel has a complete model for external writes. `Effect` has typed kinds
(`file_write`, `git_commit`, `message_send`, `artifact_publish`, `external`), a
closed state machine, and an idempotency key. `EffectService` stages, approves,
applies and reconciles effects, and it defines the `EffectAdapter` and
`ReconciliableEffectAdapter` protocols. The effects, effect_service,
approvals and effect_replay suites qualify these semantics.

The deployment host does not connect this model to anything:

- `praxis.host.app` builds `EffectService(store, authority)` with no adapters.
  Every `apply` therefore returns `effect_adapter_unavailable`.
- No module under `src/` implements `EffectAdapter`.
- No code path outside the tests calls `EffectService.stage`. A workload has no
  way to propose an effect.
- The control plane has no route to apply or reconcile an approved effect.

Knowledge publication to Noesis is the only external write that works end to
end. It uses its own `Publisher` path, not the general effect pipeline.

## Decision

1. **Effect adapters are Praxis adapters.** They sit at the same boundary as
   executor adapters. They live in a new package, `praxis.effects`, and use
   only the standard library. The host loads them from configuration. The
   kernel never imports them.
2. **An irreversible kind needs reconciliation.** An adapter for
   `message_send` or `artifact_publish` must implement
   `ReconciliableEffectAdapter`. The host refuses to start with an adapter
   that does not, so an uncertain send is never left without a lookup.
3. **Workloads propose; the kernel stages.** A workload proposes effects by
   writing files under `.praxis/effects/` in its workspace. Each file uses the
   closed, versioned schema `praxis.effect-proposal` v1. The kernel reads the
   proposals from the verified snapshot, and only when the process reaches
   `State.COMPLETED`. It then stages them under the authority of the process.
   A process that failed verification stages nothing, so an unverified result
   cannot cause an external write.
4. **Applying is an explicit, audited operation.** New routes,
   `POST /v1/processes/{id}/effects/{effect_id}/apply` and
   `.../reconcile`, need the `publish` role. They also bind to the attempt and
   the effect version, like approvals.
5. **An uncertain effect stays uncertain.** At startup, the host lists effects
   in `applying` and reports them as blocks. It never retries them. An
   operator reconciles them through the reconcile route or the runbook.

## Consequences

- Git commits, messages and artifact publications gain one path that is staged,
  approved and receipted, instead of ad hoc code in each workload.
- Adapter code runs with the privileges of the host. An operator must trust
  and pin it, as for executor adapters.
- Approval policy is a separate decision (ADR 0002). Without it, staged effects
  have no route to `pending`.

## Compatibility

- The effect schema and the SQLite effect tables do not change.
- The proposal schema is new. Unknown fields and unknown versions are rejected.
- The new routes are additive to the v1 API.
- A new `[[effects]]` table in `host.toml` selects the adapters. A host from
  before this change rejects the unknown table, which fails closed.

## Verification

- A conformance suite for effect adapters, shared by every adapter, like
  `test_all_executors`.
- A process that fails verification and writes a proposal: nothing is staged.
- An adapter that raises during `apply`: the effect stays `applying`, a retry
  is refused, and a reconcile through `lookup` finishes it.
- An adapter for an irreversible kind without `lookup`: the host refuses to
  start.
- Host tests for the apply and reconcile routes, covering roles, a stale
  version and a stale attempt.
