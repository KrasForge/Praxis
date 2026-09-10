# Runtime threat model (v1)

This model describes the trust assumptions of the runtime. It is not a claim
that it is safe to run arbitrary native code. The references TM-1 to TM-9 are
stable identifiers for the security tests and for the reviews of a deployment.

| Boundary | Trusted side | Untrusted input / attacker ability |
| --- | --- | --- |
| Kernel | The host configuration, the Python runtime, the kernel code and the installed adapters | Submitted specs, tool and model output, child requests, context content |
| Control plane | The authentication of the host and the hooks that authorize each action | Anonymous clients, an authenticated user who targets another process, forged actor fields |
| Workers | The authenticated operators of a worker and their OS isolation | Network peers, stale worker generations, replayed messages of dispatch and heartbeat |
| Executors | The adapter implementation that the host loaded | Executed programs, model prompts, output, dependency code inside the execution environment |
| Workspaces | The provider implementation and the private metadata and blobs | Candidate filenames, symlinks, traversal, concurrent writes by a running program |
| Transports | The TLS endpoint identity and the origins that the host configured | Partitions, lost acknowledgements, malformed responses, oversized bodies, redirects |
| Stores | The kernel owner and the filesystem permissions of the host | Corrupt or truncated records, interrupted writes. Other OS users must not have write access |
| Secrets | The host provider and the destination that Praxis authorized explicitly | Secret names from a workload, output that repeats a granted secret deliberately |

**TM-1.** Authority comes from the registry of grants in the kernel. A
serialized capability cannot issue authority. An executor event cannot issue
authority. A ProcessSpec cannot issue authority. A delegation must make the
scope, the actions, the limits on bytes and the expiration smaller. If you
revoke an ancestor, that revocation applies. A Python plugin in the process has
the privileges of the kernel, so you must trust it. A *native workload* that an
attacker controls is a different threat from an installed adapter that an
attacker controls.

**TM-2.** Every API request needs authentication. Authorization binds the actor,
the action and the process. The default hooks deny. An actor field in a body
cannot impersonate another actor. A host must apply the rules of ownership in
its authorization hook. It must use TLS, rate limits and request deadlines, and
it must protect the administrative endpoints. Praxis authorizes an authenticated
SSE connection when the client connects. To revoke a session, disconnect it.

**TM-3.** The checks of workspace ownership and the normalization of paths
protect the APIs of a provider. The `cwd` of a native subprocess is not
isolation. An untrusted program needs OS confinement, with a private namespace
for mounts, processes and the network, or an equivalent dedicated worker. Never
mount the controller state, a sibling workspace, credentials or the canonical
directory into an untrusted execution environment. The rejection of symlinks at
the boundaries of snapshot and import supplements OS containment. It cannot
replace that containment during execution.

**TM-4.** Completion is not verification. A canonical write needs all of these:

- A snapshot that is immutable and that a validator approved
- A baseline that is current
- Explicit authority to commit
- For remote execution, a live fence on the generation

A candidate workspace and a staged effect stay isolated until Praxis
selects a winner. A validator runs in a disposable copy. A native validator
command needs the same confinement as an executor. A validator and a rubric are
trusted policy: a validator that an attacker controls can approve bad content.

**TM-5.** Praxis stages an external effect and the policy authorizes it. To
apply an effect, Praxis needs current authority and a current state of approval.
An idempotency receipt and an explicit reconciliation handle an acknowledgement
that Praxis lost. Uncertainty never proves that the effect did not occur. Before
a replay, recovery must fence the old worker positively. A timeout during a
partition cannot authorize the relocation of work that can still be active.

**TM-6.** The identity of a worker, its generation, the attempt, the lineage of
events, the trace context, the cursors and the result envelopes must all agree.
A replay cannot make a second execution, and it cannot restore a reservation
that Praxis released. Praxis trusts an authenticated worker to report its
execution and its usage honestly. Version 1 gives no remote attestation, and it
does not protect against an operator of a worker that an attacker controls. Put
a workload that carries secrets only on a worker that you trust with those
secrets.

**TM-7.** A secret value belongs in a provider. It does not belong in a
specification or in ordinary configuration. Access needs scoped authority to
read a secret. Inject a value only into an executor destination that Praxis
authorized explicitly. Register the value for the redaction of output. Never
serialize the injection material into a dispatch, a checkpoint, an event or a
result. Redaction cannot stop a malicious workload that you authorized from
encoding a secret or sending it out. Apply isolation of the network and of
resources, and grant the least privilege.

**TM-8.** The journal and the backups are privileged data for recovery. Public
diagnostics go through redaction before an export. An error record uses a stable
code, not raw exception text. A host must register any secret value that it
injected with the redactor. It must also apply the redactor to its custom
exporters and logging handlers. Introspection of memory, root access, debugger
access and a malicious kernel plugin stay outside this protection.

**TM-9.** A protocol parser rejects an unsupported version, an invalid type, a
forged identity and an unsafe path. Resource limits bound the transport
documents that Praxis accepts, and they bound the causal chains. Containment of
CPU, memory, disk and the count of processes needs the OS controls of the host.
An application budget is a control for accounting and scheduling. It does not
replace cgroups and quotas. A live model provider and the behavior of a
third-party SDK need qualification in the deployment, beyond the offline
fixtures.

## The map of security regressions

The capability and authority tests exercise TM-1. The API authorization tests
exercise TM-2. The workspace, bundle and isolation tests exercise TM-3. The
verification, transaction and candidate tests exercise TM-4. The effect and
recovery tests exercise TM-5. The remote worker tests exercise TM-6. The secret
and redaction canaries exercise TM-7 and TM-8. The deterministic tests for
malformed input, and the compatibility suites, exercise TM-9. When you add a
regression for an attack, cite these identifiers.

## The threats that stay outside

These threats are excluded:

- A host kernel or a root account that an attacker controls
- Malicious Python packages that you installed
- Physical access
- Side channels
- The confidentiality guarantees of a provider model
- Cryptographic attestation

The owner of a deployment stays responsible for these items:

- OS patching
- TLS
- The encryption of backups
- The rotation of credentials
- The terms of a provider
- The independent containment of untrusted code
