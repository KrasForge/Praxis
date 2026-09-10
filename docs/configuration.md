# Configuration and event envelopes

## Runtime configuration

`praxis.kernel.config.load_config(path=None, environ=None, overrides=None)` returns a
frozen `Config`. Precedence runs from lowest to highest:

1. Built-in defaults
2. The explicit TOML file, if a path is given
3. `PRAXIS_`-prefixed environment variables
4. Programmatic or CLI overrides

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

The schema is closed. An unknown field raises `ConfigError("schema",
"unknown configuration field")`; an invalid value raises `ConfigError` naming the
field with a machine-readable `code`. Errors never echo the rejected value, and
`Config.to_json()` serializes only this allowlisted schema — so configuration
diagnostics are safe to export.

**Secrets are not configuration fields.** `PRAXIS_API_TOKEN` in the sample server is
host authentication material read directly by the host, not a `Config` key and not
`ProcessSpec.environment`. Credentials belong in a secret provider; see
[secrets](secrets.md).

## Event envelopes

Kernel events are version-1 JSON envelopes. Every field is required and the envelope
is closed — a document with a missing field, an unknown field, or an unsupported
`schema_version` is rejected rather than coerced.

| Field | Meaning |
| --- | --- |
| `event_id` | Unique event identity |
| `process_id` | Subject process |
| `parent_id` | Parent process identity, or `null` |
| `type` | Event type, e.g. `process.created`, `capability.issued`, `workspace.committed` |
| `payload` | JSON object; finite values only (no `NaN`/`Infinity`) |
| `timestamp` | Timezone-aware ISO 8601 |
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

Additive vendor or host data belongs in namespaced `payload` entries. Changing the
envelope itself requires explicit version support — see [versions](versions.md).

### Event types emitted by v1

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
`effect.applying` records an *uncertain* external write: it is not proof the effect
did not happen, and it must be reconciled through its idempotency receipt rather than
retried blindly.

## Cursors

Stored events carry a durable global `cursor`. Persist a cursor only *after* the
consumer has processed its event, and reconnect with it. Gaps in a filtered
per-process stream are normal, because the cursor is global. `Kernel.metrics` projects
committed events through its own stable cursor, so repeated scrapes never
double-count and a fresh runtime rebuilds identical metrics from the journal.

## Spec metadata namespaces

`ProcessSpec.metadata` is where extension data goes; it never carries authority and
never changes required contract semantics.

| Namespace | Owner |
| --- | --- |
| `metadata.codex`, `metadata.claude`, `metadata.deepseek` | Vendor adapters, which validate their allowed keys |
| `metadata.<organization_or_app>` | Host annotations, e.g. `metadata.example_app` |
| `metadata.experimental` | Experimental extensions; must name an owner and version |
| `inputs["praxis.context"]` | Reserved for kernel context injection — rejected when the spec declares `context` dependencies |

Do not add vendor flags to top-level `ProcessSpec` fields, and never infer grants from
metadata.

## Related

- [Operations](operations.md) — installing, serving, upgrading, backups
- [Metrics](metrics.md) — the metric catalog and label allowlists
- [Redaction](redaction.md) — what is scrubbed before export
- [Versions](versions.md) — wire and storage version matrix
