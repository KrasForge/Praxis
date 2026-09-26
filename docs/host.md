# Host: Praxis between Modulo and Noesis

`praxis.host` is the deployment host the kernel deliberately leaves out. It assembles
the kernel, executor registry, Noesis context provider and publication service from
one TOML file. It then serves the control plane with client authentication and TLS.

```
Modulo backend ──mTLS + bearer token──▶ Praxis host ──mTLS + nn_ API key──▶ Noesis
  (acts for users)                          │  GET  /api/v1/kb/{domain}/search   (context in)
                                            │  POST /documents/ingest           (verified results out)
```

It adds no dependencies. `serve` uses uvicorn, which remains a host dependency like
any ASGI server.

## Run it

```sh
python -m praxis.host token --client modulo     # prints a [[clients]] block; the token goes to stderr once
python -m praxis.host check --config host.toml  # validates the schema and that referenced files exist
PRAXIS_NOESIS_TOKEN=nn_... uv run --with uvicorn python -m praxis.host serve --config host.toml
```

`praxis.host.app:create_app` is also an ASGI factory, reading `PRAXIS_HOST_CONFIG`.
Embedders can call `build_host(config, executors={...})` to register agent adapters
running inside isolated workers alongside the configured `fake`/`local` executors.
The host recovers persisted records on start (`Kernel.recover_records`).

## Configuration

A complete example lives in [`examples/host.toml`](../examples/host.toml). The schema
is closed: unknown keys and unsafe combinations are rejected at load time.

| Key | Meaning |
| --- | --- |
| `data_dir` | SQLite store and workspaces (created `0700`) |
| `executors` | Built-ins to register: `fake`, `local` (Bubblewrap sandbox) |
| `server.host`, `server.port` | Bind address; default `127.0.0.1:8443` |
| `server.tls_certfile`, `server.tls_keyfile` | Serve HTTPS |
| `server.tls_client_ca` | Require client certificates signed by this CA (mutual TLS) |
| `server.tls_terminated_upstream` | TLS ends at a trusted proxy; enables proxy headers |
| `[[clients]]` | `id`, `token_sha256` (one digest, or a list of up to four during rotation), `roles`, optional `delegate` |
| `noesis.base_url` | Noesis origin; must be `https` unless loopback |
| `noesis.token_env` / `noesis.token_file` | Where the Noesis API key comes from (exactly one) |
| `noesis.ca_file`, `noesis.client_certfile`, `noesis.client_keyfile` | Private CA and client certificate for Noesis |
| `noesis.ingest_path` | Default `/documents/ingest` (Noesis `document-ingest-v1`) |
| `noesis.publication` | `off`, `manual` (default) or `auto` |
| `noesis.context_domains` | Optional allowlist of Noesis domains specs may request context from |
| `[limits]` | Optional admission limits; see [Limits and deadlines](#limits-and-deadlines) |
| `[[effects]]` | Effect adapters, one per kind; see [Effects and approvals](#effects-and-approvals) |
| `[[effect_policy]]` | Approval rules for proposed effects; deny by default |

A non-loopback bind without local TLS is refused unless
`tls_terminated_upstream = true` says a proxy owns TLS. Secrets are never
configuration values. Client tokens appear only as SHA-256 digests. The Noesis key is
read from the environment or a file and registered with the response redaction policy.

## Authentication and authorization

Clients send `Authorization: Bearer <token>`. The host hashes the token and compares
the digest against every configured client, so timing does not reveal which client
matched. A client with `delegate = true` may add `X-Praxis-On-Behalf-Of: <user>`.
The acting identity then becomes `<client>/<user>`, for example `modulo/alice@example.com`.
Submissions, approvals, interventions and publication requests are audited under that
identity. Other clients sending the header are rejected.

| Role | Allows |
| --- | --- |
| `submit` | `POST /v1/processes` |
| `read` | inspect, `tree`, `events` |
| `control` | `control`, `interventions` |
| `approve` | `approvals` (list and decide) |
| `publish` | `POST /v1/processes/{id}/publication`, effect `apply` and `reconcile`, and auto-publication opt-in |
| `health` | `GET /v1/health` |
| `admin` | every role, on every process |

Process-scoped routes also check ownership. The owner is the actor who submitted the
root of the process tree:
- A request naming a user reaches only that user's processes.
- The client acting as itself reaches all processes it or its users submitted.
- `admin` reaches everything.
- Processes created outside the API have no submitting actor and are admin-only.

A person named as an approver in an `[[effect_policy]]` rule, and whose client
has the `approve` role, can use the approvals routes on process trees they do not
own. Which effects they may decide is checked per effect.

## Effects and approvals

A process proposes effects by writing files under `.praxis/effects/` (see
[concepts](concepts.md#to-reach-outside-effects)). When it completes verified, the
host finds the most specific `[[effect_policy]]` rule for each effect: an exact
target wins over `prefix*`, and a longer prefix over a shorter one.

| Policy | What happens |
| --- | --- |
| no rule, or `deny` | The effect is staged without authority and recorded as `rejected` |
| `auto` | The host grants the process exactly the authority of the effect, and approves it. Not allowed for `message_send` or `artifact_publish` |
| `human` | The host grants the authority and queues the effect for the rule's `approvers` |

```toml
[[effect_policy]]
kind = "message_send"
target = "releases"
policy = "human"
approvers = ["modulo/*", "ops"]    # client, client/user or client/*
separate_submitter = true          # the submitter cannot approve their own effect
expires_seconds = 3600             # an approval not applied in time lapses

[[effects]]
kind = "message_send"
adapter = "webhook"
base_url = "https://hooks.internal"
token_env = "PRAXIS_WEBHOOK_TOKEN"
```

People are not processes, so approver authority comes from this configuration,
never from a kernel capability; a reload changes it at once, and a restart keeps
it (ADR 0002). Each decision is recorded with its actor. Approved effects are
applied with `POST /v1/processes/{id}/effects/{effect_id}/apply` (role
`publish`).

| Adapter | Kind | Settings |
| --- | --- | --- |
| `git` | `git_commit` | `repositories` (name to absolute path), `author_name`, `author_email`. Targets name a repository; `HEAD` must equal `expected_head`; hooks and signing are disabled; it never pushes |
| `webhook` | `message_send` | `base_url`, `send_path`, `status_path`, credential. The receiver deduplicates by `idempotency_key` and answers `GET <status_path>/<key>` |
| `http_put` | `artifact_publish` | `base_url`, `prefix`, credential. Uploads verified snapshot bytes to `<prefix>/<idempotency_key>` |

Credentials are `token_env` or `token_file` and are sent as bearer tokens; TLS
uses `ca_file`, `client_certfile` and `client_keyfile`. An adapter for an
irreversible kind must support lookup, or the host refuses to start. Adapters
and their settings change only with a restart; the policy reloads on `SIGHUP`.

At startup, the host stages again any proposals journaled before a crash; the
idempotency keys make this safe. An effect left in `applying` is reported as
`effect_uncertain` in `GET /v1/health` and on its process, and it is never
applied again. Reconcile it with
`POST /v1/processes/{id}/effects/{effect_id}/reconcile`.

## Transport security

**Inbound.** With `tls_client_ca` set, uvicorn requires a client certificate from the
internal CA before any HTTP is read. The bearer token then identifies the client.
Both are needed, so a leaked token is useless without a certificate, and a
certificate alone grants no roles. If a proxy terminates TLS instead, keep the
Praxis port reachable only from that proxy.

**Outbound to Noesis.** `HTTPTransport` accepts an `ssl.SSLContext`. The host builds
one from `noesis.ca_file` (the default trust store when omitted) with TLS ≥ 1.2, and
loads `client_certfile`/`client_keyfile` when Noesis requires mutual TLS. Noesis
authenticates API keys sent as `Authorization: Bearer nn_...`. Redirects are still
rejected, response size stays bounded, and TLS failures surface as
`transport_unavailable`.

## Context from Noesis

When `[noesis]` is configured, the provider named `noesis` is registered. Specs
request context and receive it in `inputs["praxis.context"]` with provenance:

```json
{"context": [{"context_id": "policy", "provider": "noesis",
              "request": {"query": "refund", "filters": [["domain", "support"]], "max_results": 5}}]}
```

A required dependency that Noesis cannot answer fails the process before execution.

Noesis does not yet scope API keys per domain (Ikey168/Noesis#1784), so any domain the
host's key can read is reachable from any spec. Set `noesis.context_domains` to the
domains Modulo users may read. A request for any other domain is answered locally as
`unavailable` with reason `noesis_domain_not_allowed`, without contacting Noesis, and
a required dependency then fails the process. Keep the allowlist even after Noesis
enforces key scopes; it is the host's own statement of what tasks may read.

## Publishing results to Noesis

Every path goes through `PublicationService`. Only `completed` processes whose
verification approved are published, and only with an `effect/apply` grant on
`noesis`. Losing speculative candidates are refused. Replays return `duplicate`
instead of writing twice. The document carries the full result, its lineage up to
`process.result`, and `metadata.praxis.verified`.

| Mode | Trigger |
| --- | --- |
| `off` | None; the publication route returns 404 |
| `manual` | `POST /v1/processes/{id}/publication` by a client with `publish` (a "publish" button in Modulo) |
| `auto` | Also: a submission from a `publish` client with `"metadata": {"praxis_host": {"publish": true}}` is published when it finishes |

In `auto` mode the metadata only states intent. The grant is issued because the
authenticated client holds `publish`, so a client without that role cannot opt in.
A background worker tails the durable event journal from a persisted cursor. It
queues and advances the cursor in one transaction, retries `unavailable` with
exponential backoff (capped at five minutes), and survives restarts. Results that
finished while the host was down are published on the next start, which gives you
the batch catch-up.

The manual route answers `200` for `accepted`/`duplicate`, `409` for `rejected`
(`publication_ineligible`, `publication_stale_result`, ...) or a non-terminal
process, and `503` for `unavailable`. A publication left in progress by a crash is
never retried automatically. Reconcile it with its deterministic `document_id` as
described in [operations](operations.md#recovery).

## Limits and deadlines

```toml
[limits]
requests_per_minute = 120         # per acting identity: <client> or <client>/<user>
client_requests_per_minute = 1200 # per client, all of its users together
submit_per_minute = 20            # POST /v1/processes, per acting identity
max_streams = 4                   # concurrent SSE streams per acting identity
request_timeout_seconds = 30      # non-streaming requests
exempt_admin = true               # the default
```

Every key is optional. Rates are token buckets that start full, so a rate of *N* per
minute also allows a burst of *N*. A request over a limit gets `429` with
`rate_limited` or `stream_limited` and a `Retry-After` header. A stream slot is
released when its client disconnects or the process finishes. Unauthenticated requests
are answered `401` without consuming anything. Limits live in memory; the host is a
single controller.

A request that exceeds `request_timeout_seconds` gets `503 request_timeout`, but the
operation keeps running. Cancelling a submission, control or approval midway could
leave kernel state half-applied, so only the response is abandoned. Clients re-inspect
the process to learn the result. Counts of each rejection kind are kept in
`HostApplication.limiter.rejections` and logged with the acting identity.

## Retention

```toml
[retention]
workspace_days = 14                 # terminal workspaces and unreferenced snapshots
canonical_revisions = 5             # superseded revisions kept besides the current one
canonical_roots = ["/srv/canonical"]  # needs canonical_revisions
interval_hours = 6                  # optional: sweep in the background
```

Every key is optional. Without `interval_hours` the host never sweeps by itself.
`python -m praxis.host retention --config host.toml` prints what the policy would
remove, and `--apply` removes it. The command opens only the database and the
workspaces, so the report is safe beside a running host. Use `--apply` only
while the host is stopped. A running host applies the policy through
`interval_hours`, inside its own controller. The rules are in
[operations](operations.md#retention). A change to `[retention]` needs a restart.

## Reloading without a restart

`SIGHUP` re-reads the config file given to `serve`, or `PRAXIS_HOST_CONFIG` under
another ASGI server. It applies:

- client tokens, roles and delegation;
- the server certificate and key, and additional trusted client CAs (`serve` only);
- the Noesis credential (from `token_file`; environment variables cannot change in a
  running process), CA and client certificate, swapped as one transport;
- `noesis.context_domains`.

Everything is read and validated before anything changes, so an invalid file, an
unreadable certificate or an empty credential leaves the running configuration in
place. The result is logged either way. Changes to `data_dir`, `executors`, the bind
address, the TLS mode, `[limits]` or the Noesis origin and publication mode are
refused and need a restart.

To rotate a client token without an outage:
1. List both digests (`token_sha256 = ["<new>", "<old>"]`) and send `SIGHUP`.
2. Move the client to the new token.
3. Remove the old digest and send `SIGHUP` again. The old token is rejected from then on.

New TLS handshakes use a reloaded certificate. Connections already open keep the
certificate they negotiated. OpenSSL cannot remove a trusted CA from a live context,
so dropping a client CA needs a restart.

## What Modulo calls

Modulo's backend is one delegating client. For a signed-in user it sends the mTLS
client certificate, its bearer token and the user's identity:

```sh
curl --cert modulo.pem --key modulo.key --cacert internal-ca.pem \
  -H "Authorization: Bearer $PRAXIS_MODULO_TOKEN" \
  -H "X-Praxis-On-Behalf-Of: alice@example.com" \
  -H "Idempotency-Key: ticket-42" \
  -d @spec.json https://praxis.internal:8443/v1/processes
```

It then streams `GET /v1/processes/{id}/events` (SSE, resume with `Last-Event-ID`),
shows `state` and `verification` separately, lists and decides approvals, and offers
publication through `POST /v1/processes/{id}/publication`. The routes and status
codes are in [API](api.md). Do not forward end-user identities Modulo has not
authenticated itself: Praxis trusts the delegating client for that assertion.

## Not covered

- Distributed rate limiting: limits are per host process.
- Removing a trusted client CA without a restart.
- Multi-controller placement: the host is one kernel owner, as in [operations](operations.md).
- Journal compaction: retention removes files, never process records or events.
