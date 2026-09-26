# 0002. Effect approval policy and approver authority

- **Status:** Proposed
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
  `<client>/<user>`, but it never issues that capability to them. A client with
  the `approve` role reaches the route, and the kernel then rejects the decision
  with `approval_rejected`.
- `Authority` keeps grants in memory (`kernel/authority.py`). A grant issued at
  runtime does not survive a restart.

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
4. **Configuration is the source of approver authority.** At startup and on
   `SIGHUP` reload, the host issues `EFFECT approve` capabilities to the
   approvers of each rule, scoped to its targets. It revokes capabilities for
   approvers that were removed. Because the grants come from configuration,
   they are rebuilt after a restart.
5. **Optional separation of duties.** A rule with `separate_submitter = true`
   rejects a decision from the identity that submitted the process tree.
6. **The kernel stays unchanged.** The host calls `apply_policy` right after
   staging (ADR 0001). The kernel checks and approval records stay as they
   are.

## Consequences

- The approvals route and the `approve` role do what the host documentation
  describes.
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
- A reload test: removing an approver revokes the grant, and a later decision
  by that approver fails.
- A restart test: the approver grants are present again after a restart.
- An adversarial test: with `separate_submitter`, the submitter cannot approve.
