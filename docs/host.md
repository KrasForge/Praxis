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
| `[[clients]]` | `id`, `token_sha256`, `roles`, optional `delegate` |
| `noesis.base_url` | Noesis origin; must be `https` unless loopback |
| `noesis.token_env` / `noesis.token_file` | Where the Noesis API key comes from (exactly one) |
| `noesis.ca_file`, `noesis.client_certfile`, `noesis.client_keyfile` | Private CA and client certificate for Noesis |
| `noesis.ingest_path` | Default `/documents/ingest` (Noesis `document-ingest-v1`) |
| `noesis.publication` | `off`, `manual` (default) or `auto` |

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
| `publish` | `POST /v1/processes/{id}/publication`, and auto-publication opt-in |
| `health` | `GET /v1/health` |
| `admin` | every role, on every process |

Process-scoped routes also check ownership. The owner is the actor who submitted the
root of the process tree:
- A request naming a user reaches only that user's processes.
- The client acting as itself reaches all processes it or its users submitted.
- `admin` reaches everything.
- Processes created outside the API have no submitting actor and are admin-only.

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
Noesis enforces its own domain authorization for the host's key. Scope that key to
the domains Modulo users may read.

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

- Rate limits and request deadlines: put these in a proxy.
- Revoking a token: remove or replace its digest and restart.
- Rotating certificates: replace the files and restart.
- Multi-controller placement: the host is one kernel owner, as in [operations](operations.md).
