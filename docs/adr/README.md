# Architecture decision records

Use `NNNN-short-title.md` for decisions that establish or reverse a durable
kernel boundary, authority rule, execution invariant, wire/storage contract, or
operational ownership rule. Each ADR records status, context, decision,
consequences, compatibility, verification, and superseded decisions. Accepted
records are historical evidence: supersede them instead of silently rewriting
the decision.

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-effect-adapters.md) | Effect adapters and the host effect pipeline | Accepted |
| [0002](0002-effect-approval-policy.md) | Effect approval policy and approver authority | Accepted |
| [0003](0003-planning-as-a-verified-workload.md) | Planning runs as a verified workload | Accepted |
| [0004](0004-single-controller-ownership.md) | A single controller owns each store | Accepted |
| [0005](0005-recover-staged-candidate-transactions.md) | Recover staged candidate transactions from the journal | Accepted |
| [0006](0006-retention-excludes-the-journal.md) | Retention removes files, never the journal | Accepted |
