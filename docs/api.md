# Control-plane API

Host `praxis.api.asgi.Application(ControlPlane(kernel), SecurityHooks(...))` on
an ASGI server. By default, Praxis denies anonymous access. Authentication
returns an Actor. Authorization receives the actor, the action and the identity
of the process. The host owns the rules for the ownership of a tenant and a
process. A value for the actor in a body cannot replace the identity that Praxis
authenticated. The sample server has one operator and a fake executor, on
purpose.

| Method/path | Operation |
| --- | --- |
| POST /v1/processes | Submit a ProcessSpec. The scope of Idempotency-Key is the actor |
| GET /v1/processes/{id} | Read the state, result, verification, usage, effects, last event and reason for a block |
| GET /v1/processes/{id}/tree | Inspect the subtree, read-only |
| GET /v1/processes/{id}/events | SSE, with an after cursor or a Last-Event-ID cursor, and an optional tree=true |
| POST /v1/processes/{id}/control | Give an operation and the current attempt_id: cancel, suspend, resume, signal or retry |
| GET/POST /v1/processes/{id}/approvals | Read the pending effects, or give a decision that is bound to an attempt and a version |
| POST /v1/processes/{id}/interventions | Give an audited instruction or signal intervention that has a type |
| GET /v1/health | Read a snapshot of the queue, executors, workers, blocks and budget pressure |

A submission returns 202, or 200 for a duplicate. Validation returns 422. A
missing identity returns 404. A mutation that is stale or that conflicts returns
409. Authentication returns 401. A denial of authority returns 403. An error
uses an error object that has a code and details.

Praxis can accept an executor control and still report `supported=false` or
`applied=false`. A JSON body is bounded to 1 MiB.

An SSE cursor is a durable global position. A gap in a stream that is filtered to
one process is normal. Persist the cursor only after you process each event, and
then reconnect with it.

An approval body contains `effect_id`, `version`, `attempt_id`, `approved` (a
boolean) and `reason`. The validation of the service controls the decisions that
it accepts. No API credential gives executor authority or effect authority
automatically. The internal methods of ControlPlane are trusted APIs for a host,
and they go around the security hooks of HTTP.

Modulo should use the [typed client](client.md) for these contracts. It should
also do all of these:

- Keep the IDs of the process and the attempt
- Show the verification apart from the execution
- Show an unsupported control explicitly
- Use the audit identities of the API

No database of Modulo and no interface of Modulo is coupled to Praxis. Noesis
publication stays a separate service of the host.

A direct submission starts on the kernel immediately. A deployment that needs a
queue or distributed placement has two options. It can send the submission
through the scheduler integration of its host. It can also configure remote
executors explicitly.

Attach `RuntimeHealth(kernel, scheduler, heartbeats)` to ControlPlane to include
the true state of the queue and the workers. Without those dependencies, the
snapshot reports only the registry of the kernel. The presence of a registry
entry does not probe an SDK installation, and it does not probe the availability
of a provider. A health read never renews a lease and never starts work.
