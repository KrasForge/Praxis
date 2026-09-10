# Configuration and event envelopes

## Runtime configuration

`praxis.kernel.config.load_config(path=None, environ=None, overrides=None)`
returns a frozen `Config`. The precedence goes from the lowest to the highest:

1. The built-in defaults
2. The explicit TOML file, if you give a path
3. The environment variables with the `PRAXIS_` prefix
4. The overrides from your program or from the CLI

| Key | Type | Default | Environment variable |
| --- | --- | --- | --- |
| `storage_path` | nonempty path | `praxis.db` | `PRAXIS_STORAGE_PATH` |
| `workspace_root` | nonempty path | `.praxis/workspaces` | `PRAXIS_WORKSPACE_ROOT` |
| `log_level` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` | `INFO` | `PRAXIS_LOG_LEVEL` |
| `max_concurrency` | positive integer | `4` | `PRAXIS_MAX_CONCURRENCY` |

```toml
# praxis.toml
storage_path = "/var/lib/praxis/runtime.db"
workspace_root = "/var/lib/praxis/workspaces"
log_level = "WARNING"
max_concurrency = 16
```

```python
from pathlib import Path
from praxis.kernel.config import ConfigError, load_config

config = load_config(Path("praxis.toml"), overrides={"max_concurrency": 8})
```

The schema is closed. An unknown field raises `ConfigError("schema", "unknown
configuration field")`. An invalid value raises `ConfigError` that names the
field and gives a `code` that a machine can read. An error never repeats the
value that Praxis rejected. `Config.to_json()` serializes only this allowlisted
schema. Thus you can export the diagnostics for the configuration safely.

**A secret is not a configuration field.** In the sample server,
`PRAXIS_API_TOKEN` is authentication material that the host reads directly. It
is not a `Config` key, and it is not `ProcessSpec.environment`. Put credentials
in a secret provider. See [secrets](secrets.md).

## Event envelopes

A kernel event is a JSON envelope of version one. Every field is required and
the envelope is closed. Praxis rejects a document that has a missing field, an
unknown field, or a `schema_version` that it does not support. It does not
coerce the document.

| Field | Meaning |
| --- | --- |
| `event_id` | The unique identity of the event |
| `process_id` | The process that is the subject |
| `parent_id` | The identity of the parent process, or `null` |
| `type` | The type of the event, for example `process.created`, `capability.issued` or `workspace.committed` |
| `payload` | A JSON object with finite values only. `NaN` and `Infinity` are not permitted |
| `timestamp` | ISO 8601, with a time zone |
| `schema_version` | `1` |

```json
{
  "event_id": "0f0b...",
  "process_id": "5c2a...",
  "parent_id": null,
  "type": "process.created",
  "payload": {"actor": "operator"},
  "timestamp": "2026-09-09T03:11:21.482913+00:00",
  "schema_version": 1
}
```

Put additive data from a vendor or a host in a namespaced entry of the
`payload`. To change the envelope, you must add explicit support for a version.
See [versions](versions.md).

### The event types of v1

| Group | Types |
| --- | --- |
| Process | `process.created`, `process.state`, `process.outcome`, `process.result`, `process.control`, `process.retry`, `process.intervention`, `process.recovery` |
| Authority | `capability.issued`, `capability.delegated`, `capability.revoked`, `capability.decision` |
| Workspace | `workspace.committed`, `workspace.rolled_back` |
| Effects | `effect.applying`, `effect.applied` |
| Workers | `worker.registered`, `worker.placed`, `worker.dispatch_pending`, `worker.execution_started`, `worker.execution_completed`, `worker.execution_fenced`, `worker.heartbeat` |
| Accounting | `usage.recorded` |
| Knowledge | `knowledge.published` |

`workspace.committed` is the only event that proves canonical publication.

`effect.applying` records an external write that is **uncertain**. It is not
proof that the effect did not occur. Reconcile it through its idempotency
receipt. Do not retry it blindly.

## Cursors

A stored event carries a durable global `cursor`. Persist a cursor only *after*
the consumer processed its event, and then reconnect with it. A gap in a stream
that is filtered to one process is normal, because the cursor is global.

`Kernel.metrics` projects the committed events through its own stable cursor.
Thus repeated scrapes never count an event twice, and a new runtime rebuilds
identical metrics from the journal.

## The metadata namespaces of a spec

`ProcessSpec.metadata` holds the data for an extension. It never carries
authority, and it never changes the required semantics of a contract.

| Namespace | Owner |
| --- | --- |
| `metadata.codex`, `metadata.claude`, `metadata.deepseek` | The vendor adapters, which check their permitted keys |
| `metadata.<organization_or_app>` | The annotations of a host, for example `metadata.example_app` |
| `metadata.experimental` | An experimental extension. It must name an owner and a version |
| `inputs["praxis.context"]` | Reserved for the context injection of the kernel. Praxis rejects it if the spec declares `context` dependencies |

Do not put a vendor flag in a top-level field of `ProcessSpec`. Never infer a
grant from metadata.

## Related

- [Operations](operations.md): how to install, serve, upgrade and back up
- [Metrics](metrics.md): the metric catalog and the label allowlists
- [Redaction](redaction.md): what Praxis removes before an export
- [Versions](versions.md): the version matrix for the wire and the storage
