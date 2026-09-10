# Praxis documentation

Praxis owns execution semantics: processes, authority, verification and recovery. The
central invariant is that failed or unverified work can never be represented as
successful committed work. These pages state how that is enforced and what a host
must do to keep it true.

## Start here

| | |
| --- | --- |
| [Concepts](concepts.md) | The execution model end to end, with runnable code. Read this first. |
| [Architecture](architecture.md) | Ownership boundaries and failure semantics, stated precisely. |

## Building on Praxis

| | |
| --- | --- |
| [Control-plane API](api.md) | HTTP routes, status codes, idempotency, SSE cursors, host security hooks. |
| [Client SDK](client.md) | The typed Python v1 client: submit, inspect, control, stream. |
| [Configuration](configuration.md) | TOML keys and precedence, event envelopes and types, metadata namespaces. |

## Running Praxis

| | |
| --- | --- |
| [Operations](operations.md) | Install, serve, register executors, upgrade, back up, recover. |
| [Metrics](metrics.md) | The stable metric catalog: names, units, kinds, label allowlists. |
| [Qualification](qualification.md) | v1.0.0 release evidence, the reproducible gate, deployment limits. |

## Security

Read these before hosting native workloads or connecting a live provider.

| | |
| --- | --- |
| [Threat model](threat-model.md) | TM-1..TM-9 trust boundaries, the security regression map, excluded threats. |
| [Isolation](isolation.md) | Sandbox construction, runtime mounts, isolated-worker requirements, output limits. |
| [Secrets](secrets.md) | Provider integration, scoped secret authority, injection boundaries. |
| [Redaction](redaction.md) | What is scrubbed before bytes leave the controller, and what is not. |

## Compatibility

| | |
| --- | --- |
| [Versions](versions.md) | Wire/storage version matrix, negotiation, compatibility and deprecation policy, persisted layouts. |

## Conventions in these documents

- **Outcome** describes what an executor did; **state** describes what the kernel
  concluded. They are never used interchangeably.
- **TM-*n*** references are stable identifiers from the [threat model](threat-model.md).
  Security regression tests should cite them.
- Statements about what a *host* must do are requirements on the embedding
  application, not behavior Praxis provides.
