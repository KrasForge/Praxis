# v1 version matrix

Package versions describe releases. Wire schema versions describe document semantics. SQLite user_version describes storage layout. These numbers are independent.

| Component | Version field / declaration | Read | Write |
| --- | --- | --- | --- |
| ProcessSpec | schema_version | 1 | 1 |
| ProcessResult | schema_version | 1 | 1 |
| Event | schema_version | 1 | 1 |
| Executor | descriptor/request protocol_version | 1 | 1 |
| Workspace | provider protocol_version | 1 | 1 |
| Capability | schema_version | 1 | 1 |
| Contract | schema_version | 1 | 1 |
| Effect | schema_version | 1 | 1 |
| Worker | registration/dispatch protocol_version | 1 | 1 |

`praxis.compatibility.VERSIONS` is the machine-readable matrix. `negotiate(component, offered)` chooses the highest supported common version, rejecting booleans, empty or malformed offers and unknown components. Worker registration accepts either the original single `protocol_version` or `protocol_versions: [1]`; its returned worker record contains the chosen version. Dispatch must use that version and current generation. Negotiation never changes persisted documents or silently translates semantics. Each executor feature must also be explicitly advertised; a matching protocol version does not imply checkpoint/cancel support.

Top-level schemas are closed: unknown fields fail validation. Optional fields with specified defaults support older v1 documents. Forward reading of arbitrary newer fields is not promised. Senders targeting a known older reader must omit unsupported optional fields or use the common negotiated schema. No v2 reader or converter is claimed today.

Vendor controls belong under metadata.codex, metadata.claude, or metadata.deepseek and their adapters validate the allowed keys. Host annotations belong under metadata using an organization/application key, for example metadata.example_app. Experimental extensions use metadata.experimental with an owner and version. These namespaces carry data, never new authority or changes to required contract semantics. Do not add vendor flags to ProcessSpec top-level fields or infer grants from metadata. Stable core context inputs use the reserved praxis.context namespace.

## Compatibility and deprecation policy

Within a supported wire version, existing required fields retain their meanings, units, authorization requirements and defaults. New readers must read the oldest supported fixtures. Optional fields may be added only when omission preserves old behavior; because readers reject unknown fields, a new writer must not send them to an older reader without a negotiated capability. Package patch releases do not intentionally break documented public Python signatures. Internal underscored APIs and experimental namespaces carry no such promise.

Breaking changes include deleting or renaming fields, changing defaults or units, accepting previously forbidden authority, changing terminal-state/verification semantics, adding required fields, or dropping supported protocol pairs. They require a new schema/protocol version and explicit migration/negotiation. Security fixes may tighten malformed or unsafe input validation without retaining the vulnerable behavior; release notes must identify the change. Database upgrades use ordered transactions; wire negotiation is not a database migration.

A deprecation starts with release notes naming the replacement, a migration example, and a removal release. Public Python entry points call `warn_deprecated(feature, replacement=..., removal=...)`, which emits `PraxisDeprecationWarning` (a visible FutureWarning). Hosts can promote it to an error with `warnings.simplefilter("error", PraxisDeprecationWarning)` in compatibility CI. Notices contain public identifiers only. No current v1 feature is deprecated; the helper is the mechanism for future notices, not a warning on every normal call.

Removal requires at least two minor releases and 90 days after the first published notice, and the next breaking protocol/package major where applicable. Maintain old readers and regression fixtures through that window. Experimental features may change sooner if their explicit owner/version changes. If a replacement is not available, removal must be postponed or identified as a documented security exception.

Examples: adding an optional result error with a default of null is backward-readable by the new reader, while old readers need the field omitted. Changing cost_microusd to floating dollars is a new schema version. A worker offering [2, 1] selects 1 today; a peer offering only [2] fails explicitly. Moving a Codex model option into the core schema is not a compatible vendor extension. Unsupported versions must never be interpreted as v1 by stripping their version fields.

## Persisted layouts

SQLite layout 1 contains processes, ordered events and checkpoints. Layout 2 adds an event lookup index and migration history; process/event JSON and cursors remain unchanged. SQLiteStore automatically upgrades supported layouts 0 (empty) and 1 to 2 under one BEGIN IMMEDIATE transaction. A failed statement rolls back data, DDL and user_version together. Unknown future layouts are rejected; no downgrade is attempted. Optional subsystem tables remain owned by their services and are preserved by these migrations.

Back up the complete quiesced runtime before upgrading. Keep the old package and backup until the upgraded runtime passes inspection and qualification. Never point an older binary at an upgraded database: restore the backup as a separate recovery operation. Migration authors must append contiguous versions, use individual transactional statements, preserve historical fixtures, and include failure-injection tests. Do not use executescript or external side effects inside a migration.
