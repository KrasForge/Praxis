# 0002. Effect approval policy and approver authority

- **Status:** Accepted
- **Date:** 2026-09-26
- **Supersedes:** none

## Context

`EffectService` supports three policies, `auto`, `human` and `deny`, through
`apply_policy`. It records each decision as an `ApprovalRecord`, with the actor,
the reason, the effect version and an optional expiry. The host exposes
`GET/POST /v1/processes/{id}/approvals` behind the `approve` role.

The pieces are not connected:

- Nothing outside the tests calls `apply_policy`. A staged effect never enters
  `pending_approvals`, so the approvals route always lists nothing.
- `resolve_approval` requires that the actor holds an `EFFECT approve`
  capability for the target. The host authenticates people as
  `<client>/<user>`, and a person is not a process. `Authority` journals every
  grant and every decision under the process ID of the recipient
  (`kernel/authority.py`, `storage/journal.py`). So a person cannot hold a
  kernel capability, and the check itself fails when it journals its decision.
  A client with the `approve` role reaches the route, and the decision is then
  rejected with `approval_rejected`.

Two checks are correct, and this decision keeps them: the host role and the
kernel capability. The host role decides who can call the route. The kernel
capability decides what that caller can approve.

## Decision

1. **Declarative policy in `host.toml`.** Each `[[effect_policy]]` rule
   matches an effect kind and a target pattern. It names a policy (`auto`,
   `human` or `deny`), the approvers (client identities or `<client>/<user>`
   patterns), and an optional `expires_seconds` for approvals.
2. **Deny by default.** An effect that matches no rule is rejected at staging.
3. **No automatic approval of irreversible effects.** A rule cannot give `auto`
   to `message_send` or `artifact_publish`. The configuration is rejected.
4. **Configuration is the source of approver authority.** `EffectService`
   accepts an optional `ApproverPolicy`, which answers whether an actor may
   decide on a given effect. When it is set, `resolve_approval` asks the policy
   instead of the kernel registry, for actors that are not processes. The host
   implements it from the rules. A process actor still needs an `EFFECT
   approve` capability, as before. Nothing is stored, so the policy is current
   after every reload and every restart. The decision itself stays audited: the
   `effect.approved` or `effect.rejected` event is journaled under the process
   of the effect, and the `ApprovalRecord` names the actor.
5. **Optional separation of duties.** A rule with `separate_submitter = true`
   rejects a decision from the identity that submitted the process tree.
6. **The kernel change is small.** The host calls `apply_policy` right after
   staging (ADR 0001). The only kernel change is the optional
   `ApproverPolicy`. Without it, the kernel behaves as it does today.

## Consequences

- The approvals route and the `approve` role do what the host documentation
  describes.
- The authority of a person comes from host configuration, not from the kernel
  registry. The kernel registry stays limited to processes.
- A person approves an effect only when the operator named them in the
  configuration. An API role alone never grants effect authority, as
  `docs/api.md` already requires.
- The approval records give an audit trail per person, readable through the
  existing approvals route. The journal stays privileged recovery data.

## Compatibility

- `[[effect_policy]]` is a new table in `host.toml`. Older hosts reject it.
- No change to the wire format, the effect schema or the storage layout.

## Verification

- A policy matrix test: each kind with `auto`, `human`, `deny` and no rule.
- A configuration test: `auto` on an irreversible kind is rejected.
- A host test: an approver named in the configuration approves through the
  route; a client with the role but no rule is rejected.
- A reload test: after an approver is removed, a decision by that approver
  fails.
- A restart test: an approver named in the configuration can decide after a
  restart.
- A kernel test: without an `ApproverPolicy`, a person cannot approve.
- An adversarial test: with `separate_submitter`, the submitter cannot approve.
