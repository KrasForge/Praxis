# Praxis documentation

Praxis owns the semantics of execution: processes, authority, verification and
recovery. The central invariant is this: work that failed, and work that Praxis
did not verify, can never look like work that succeeded and that Praxis
committed. These pages give the rules that hold that invariant true, and the
duties of a host that keeps it true.

## Start here

| | |
| --- | --- |
| [Concepts](concepts.md) | The execution model, from end to end, with code you can run. Read this first. |
| [Architecture](architecture.md) | The boundaries of ownership and the failure semantics, given precisely. |

## To build on Praxis

| | |
| --- | --- |
| [Control-plane API](api.md) | The HTTP routes, the status codes, the idempotency, the SSE cursors and the security hooks of a host. |
| [Client SDK](client.md) | The typed Python client for v1: how to submit, inspect, control and stream. |
| [Configuration](configuration.md) | The TOML keys and their precedence, the event envelopes and types, and the metadata namespaces. |

## To run Praxis

| | |
| --- | --- |
| [Operations](operations.md) | How to install, serve, register executors, upgrade, back up and recover. |
| [Metrics](metrics.md) | The stable metric catalog: the names, units, kinds and label allowlists. |
| [Qualification](qualification.md) | The v1.0.0 release evidence, the reproducible gate and the deployment limits. |

## Security

Read these pages before you host a native workload or connect a live provider.

| | |
| --- | --- |
| [Threat model](threat-model.md) | The trust boundaries TM-1 to TM-9, the map of security regressions, and the threats that stay outside. |
| [Isolation](isolation.md) | The sandbox, the runtime mounts, the rules for an isolated worker, and the output limits. |
| [Secrets](secrets.md) | How to integrate a provider, how to scope secret authority, and the limits of injection. |
| [Redaction](redaction.md) | What Praxis removes before bytes leave the controller, and what it cannot remove. |

## Compatibility

| | |
| --- | --- |
| [Versions](versions.md) | The version matrix for the wire and the storage, the negotiation, the policy for compatibility and deprecation, and the stored layouts. |

## The conventions of these documents

- An **outcome** describes what an executor did. A **state** describes what the
  kernel concluded. These documents never use one word for the other.
- A **TM-*n*** reference is a stable identifier from the
  [threat model](threat-model.md). A security regression test must cite one.
- A statement about what a *host* must do is a requirement on the application
  that embeds Praxis. It is not behavior that Praxis supplies.
