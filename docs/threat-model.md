# Runtime threat model (v1)

This model describes the runtime's trust assumptions, not a claim that running arbitrary native code is safe. References TM-1 through TM-9 are stable identifiers for security tests and deployment reviews.

| Boundary | Trusted side | Untrusted input / attacker ability |
| --- | --- | --- |
| Kernel | Host configuration, Python runtime, kernel code and installed adapters | Submitted specs, tool/model output, child requests, context content |
| Control plane | Host authentication and per-action authorization hooks | Anonymous clients, authenticated users targeting another process, forged actor fields |
| Workers | Authenticated worker operators and their OS isolation | Network peers, stale worker generations, replayed dispatch and heartbeat messages |
| Executors | Adapter implementation loaded by host | Executed programs, model prompts, output, dependency code inside the execution environment |
| Workspaces | Provider implementation and private metadata/blobs | Candidate filenames, symlinks, traversal, concurrent writes by a running program |
| Transports | TLS endpoint identity and host-configured origins | Partitions, lost acknowledgements, malformed responses, oversized bodies, redirects |
| Stores | Kernel owner and host filesystem permissions | Corrupt/truncated records, interrupted writes; other OS users must not have write access |
| Secrets | Host provider and explicitly authorized destination | Secret names from workloads; output deliberately echoing a granted secret |

TM-1: Authority comes from the kernel's grant registry. A serialized capability, an executor event, or a ProcessSpec cannot issue authority. Delegation must narrow scope, actions, byte constraints, and expiration; ancestor revocation applies. In-process Python plugins have kernel privileges and must be trusted. A malicious *native workload* is different from a malicious installed adapter.

TM-2: Authentication is required for every API request. Authorization binds actor, action, and process. Default hooks deny. Body actor fields cannot impersonate another actor. Hosts must enforce ownership in their authorization hook, use TLS, rate limits and request deadlines, and protect administrative endpoints. An authenticated SSE connection is authorized at connection time; disconnect it when revoking sessions.

TM-3: Workspace ownership checks and path normalization protect provider APIs. Native subprocess cwd alone is not isolation. Untrusted programs require OS confinement with a private mount/process/network namespace or an equivalent dedicated worker. Never mount controller state, sibling workspaces, credentials, or the canonical directory into an untrusted execution environment. Symlink rejection at snapshot/import boundaries supplements OS containment; it cannot replace it during execution.

TM-4: Completion is not verification. Canonical writes require a validator-approved immutable snapshot, a current baseline, explicit commit authority, and (for remote execution) a live generation fence. Candidate workspaces and staged effects remain isolated until winner selection. Validators execute in disposable copies; native validator commands require the same confinement as executors. Validators and rubrics are trusted policy; a compromised validator can approve bad content.

TM-5: External effects are staged and policy-authorized. Applying an effect requires current authority and approval state. Idempotency receipts and explicit reconciliation handle lost acknowledgements; uncertainty never proves non-execution. Recovery must positively fence old workers before replay. A partition timeout alone cannot authorize relocation of possibly active work.

TM-6: Worker identity, generation, attempt, event lineage, trace context, cursors and result envelopes must agree. Replays cannot create a second execution or resurrect a released reservation. Authenticated workers are trusted to report execution and usage honestly; v1 does not provide remote attestation or protect against a compromised worker operator. Secret-bearing workloads must use a worker trusted for those secrets.

TM-7: Secret values belong in a provider, not specifications or ordinary config. Access requires scoped secret-read authority. Inject only into an explicitly authorized executor destination, register values for output redaction, and never serialize injection material into dispatch, checkpoints, events, or results. Redaction cannot stop an authorized malicious workload from encoding or exfiltrating a secret; enforce network/resource isolation and grant least privilege.

TM-8: The journal and backups are privileged recovery data. Public diagnostics pass through redaction before export. Error records use stable codes rather than raw exception text. Hosts must register any externally injected secret values with the redactor and apply it to custom exporters and logging handlers. Memory introspection, root access, debugger access and malicious kernel plugins are outside this protection.

TM-9: Protocol parsers reject unsupported versions, invalid types, forged identities and unsafe paths. Resource limits bound accepted transport documents and causal chains. CPU, memory, disk and process-count containment require host OS controls; application budgets are accounting and scheduling controls, not a replacement for cgroups/quotas. Live model providers and third-party SDK behavior require deployment qualification beyond offline fixtures.

## Security regression map

Capability and authority tests exercise TM-1; API authorization tests TM-2; workspace, bundle and isolation tests TM-3; verification, transaction and candidate tests TM-4; effects and recovery tests TM-5; remote worker tests TM-6; secret and redaction canaries TM-7/TM-8; deterministic malformed-input and compatibility suites TM-9. Tests should cite these identifiers when adding attack regressions.

Excluded threats: compromised host kernel/root, malicious installed Python packages, physical access, side channels, provider model confidentiality guarantees, and cryptographic attestation. Deployment owners remain responsible for OS patching, TLS, backup encryption, credential rotation, provider terms, and independent containment of untrusted code.
