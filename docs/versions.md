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
