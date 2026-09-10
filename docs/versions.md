# v1 version matrix

A package version describes a release. A wire schema version describes the
semantics of a document. The SQLite `user_version` describes the layout of the
storage. These numbers are independent.

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

`praxis.compatibility.VERSIONS` is the matrix that a machine can read.
`negotiate(component, offered)` chooses the highest common version that Praxis
supports. It rejects booleans, an empty offer, a malformed offer and an unknown
component.

Worker registration accepts the original single `protocol_version`, or
`protocol_versions: [1]`. The worker record that it returns contains the chosen
version. A dispatch must use that version and the current generation.

Negotiation never changes a stored document, and it never translates semantics
silently. An executor must also advertise each feature explicitly. A protocol
version that agrees does not imply support for a checkpoint or for a
cancellation.

A top-level schema is closed: an unknown field fails validation. An optional
field that has a specified default supports an older v1 document. Praxis does
not promise to read arbitrary newer fields. A sender that targets a known older
reader has two options. It can omit the optional fields that the reader does not
support. It can also use the common negotiated schema. Praxis claims no v2
reader and no converter today.

A vendor control belongs under `metadata.codex`, `metadata.claude` or
`metadata.deepseek`, and its adapter checks the permitted keys. A host
annotation belongs under `metadata` with a key for the organization or the
application, for example `metadata.example_app`. An experimental extension uses
`metadata.experimental` with an owner and a version.

These namespaces carry data. They never carry new authority, and they never
change the required semantics of a contract. Do not put a vendor flag in a
top-level field of ProcessSpec. Never infer a grant from metadata. A stable core
context input uses the reserved namespace `praxis.context`.

## The policy for compatibility and deprecation

Inside a supported wire version, an existing required field keeps its meaning,
its units, its requirements for authorization and its default. A new reader must
read the oldest fixtures that Praxis supports.

You can add an optional field only when its omission keeps the old behavior. A
reader rejects an unknown field. Thus a new writer must not send that field to
an older reader without a capability that both sides negotiated. A patch release
of the package does not break a documented public Python signature on purpose.
An internal API with an underscore, and an experimental namespace, carry no such
promise.

These changes are breaking:

- To delete a field, or to rename one
- To change a default, or to change a unit
- To accept authority that Praxis forbade before
- To change the semantics of a terminal state, or of verification
- To add a required field
- To drop a supported pair of protocols

Each one needs a new schema version or protocol version. Each one also needs an
explicit migration or negotiation.

A security fix can make the validation of malformed or unsafe input stricter,
and it does not have to keep the vulnerable behavior. The release notes must
identify that change.

A deprecation starts with release notes that name the replacement, a migration
example and the release that removes the feature. A public Python entry point
calls `warn_deprecated(feature, replacement=..., removal=...)`. That emits a
`PraxisDeprecationWarning`, which is a visible FutureWarning. A host can make it
an error with `warnings.simplefilter("error", PraxisDeprecationWarning)` in its
CI for compatibility. A notice contains public identifiers only. No v1 feature
is deprecated today. The helper is the mechanism for a future notice. It is not
a warning on an ordinary call.

A removal needs all of these:

- At least two minor releases
- At least 90 days after the first published notice
- The next breaking major version of the protocol or the package, where that
  applies

Keep the old readers and the regression fixtures through that window. An
experimental feature can change sooner if its explicit owner or version
changes. If a replacement is not available, you must postpone
the removal, or identify it as a documented exception for security.

Examples. To add an optional result error with a default of null is
backward-readable by the new reader, but an old reader needs that field omitted.
To change `cost_microusd` into floating dollars is a new schema version. A
worker that offers `[2, 1]` selects 1 today, and a peer that offers only `[2]`
fails explicitly. To move an option for a Codex model into the core schema is
not a compatible vendor extension. Never interpret an unsupported version as v1
by removing its version fields.

## The stored layouts

SQLite layout 1 contains the processes, the ordered events and the checkpoints.
Layout 2 adds an index to look up an event, and a history of migrations. The
JSON of the processes and events, and the cursors, do not change.

SQLiteStore upgrades the supported layouts 0 (empty) and 1 to layout 2, under
one `BEGIN IMMEDIATE` transaction. If a statement fails, the rollback covers the
data, the DDL and the `user_version` together. Praxis rejects an unknown future
layout, and it attempts no downgrade. An optional table of a subsystem stays
owned by its service, and these migrations keep it.

Back up the complete quiesced runtime before you upgrade. Keep the old package
and the backup until the upgraded runtime passes inspection and qualification.
Never point an older binary at an upgraded database. Restore the backup as a
separate operation for recovery.

An author of a migration must append contiguous versions, use individual
transactional statements, keep the historical fixtures, and include tests that
inject a failure. Do not use `executescript` inside a migration, and do not
cause an external side effect there.
