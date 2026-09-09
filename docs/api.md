# Control-plane API

Host `praxis.api.asgi.Application(ControlPlane(kernel), SecurityHooks(...))` on an ASGI server. Anonymous access is denied by default. Authentication returns Actor; authorization receives actor/action/process identity. The host owns tenant/process ownership rules. Body actor values cannot override the authenticated identity. The sample server is intentionally one operator and a fake executor.

| Method/path | Operation |
| --- | --- |
| POST /v1/processes | Submit a ProcessSpec; Idempotency-Key is scoped to actor |
| GET /v1/processes/{id} | State, result, verification, usage, effects, last event, blocking reason |
| GET /v1/processes/{id}/tree | Read-only subtree inspection |
| GET /v1/processes/{id}/events | SSE; after or Last-Event-ID cursor, optional tree=true |
| POST /v1/processes/{id}/control | operation and current attempt_id; cancel/suspend/resume/signal/retry |
| GET/POST /v1/processes/{id}/approvals | Pending effects / attempt-and-version-bound approval decision |
| POST /v1/processes/{id}/interventions | Audited typed instruction or signal intervention |
| GET /v1/health | Read-only queue/executor/worker/blocked/budget-pressure snapshot |

Submission returns 202 or 200 for a duplicate. Validation returns 422, missing identity 404, stale/conflicting mutation 409, authentication 401 and denied authority 403. Errors use an error object with code/details. Accepted executor controls can still report supported=false or applied=false. JSON bodies are bounded to 1 MiB. SSE cursors are durable global positions; gaps are normal for filtered process streams. Persist the cursor only after processing each event and reconnect with it.

Approval bodies contain effect_id, version, attempt_id, approved (boolean) and reason; service validation controls exact accepted decisions. No API credential grants executor or effect authority automatically. Internal ControlPlane methods are trusted host APIs and bypass HTTP security hooks.

Modulo should use the [typed client](client.md) for these contracts, preserving process/attempt IDs, showing verification separately from execution, rendering unsupported controls explicitly and using API audit identities. No Modulo database or UI implementation is coupled to Praxis. Noesis publication remains a separate host service. Direct submission starts on the kernel immediately; deployments that need queue/distributed placement must route submission through their host scheduler integration or configure remote executors explicitly.

Attach RuntimeHealth(kernel, scheduler, heartbeats) to ControlPlane to include actual queue and worker state. Without attached dependencies the snapshot reports only the kernel registry. Registry presence alone does not probe an SDK installation or provider availability. Health reads never renew leases or launch work.
